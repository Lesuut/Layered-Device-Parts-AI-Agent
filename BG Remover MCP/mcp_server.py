#!/usr/bin/env python3
"""
MCP-сервер: локальное удаление фона, на выходе PNG с прозрачностью.

Инструменты:
  bg_check      - что доступно: библиотеки, движок ИИ, кеш моделей
  bg_remove     - убрать фон у файлов/папки/маски -> PNG с альфой
  bg_install_ai - доставить rembg+onnxruntime (нужен для method=ai)

Запуск как MCP (stdio):
  python mcp_server.py

Регистрация в Claude Code:
  claude mcp add bg-remover -- python "<путь>/mcp_server.py"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bg_remove as br  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]

mcp = _Server(
    "bg-remover",
    instructions=(
        "Локальное удаление фона: картинка -> PNG с прозрачностью. "
        "Движок white режет белый фон без нейросети и без интернета (быстро, "
        "точно на студийных/сгенерированных картинках). Движок ai (rembg/U^2-Net) "
        "берёт любой фон, но требует установки. По умолчанию method=auto: "
        "белые края кадра -> white, иначе -> ai. Вызов синхронный и возвращается, "
        "когда файлы уже лежат на диске."
    ),
)


@mcp.tool()
def bg_check() -> str:
    """Проверить окружение удаления фона.

    Показывает версии pillow/numpy/scipy, доступен ли ИИ-движок (rembg +
    onnxruntime), какие модели уже скачаны. Движок white работает всегда.
    """
    return json.dumps(br.check_environment(), ensure_ascii=False, indent=2)


@mcp.tool()
def bg_remove(
    images: Annotated[
        list[str],
        Field(
            description="Что резать: абсолютные пути к файлам, путь к папке "
            "или маска вида C:\\images\\*.jpeg. Можно смешивать"
        ),
    ],
    out_dir: Annotated[
        str | None,
        Field(description="Куда класть PNG. По умолчанию рядом с исходником"),
    ] = None,
    method: Annotated[
        str,
        Field(
            description="auto (по умолчанию), white — белый фон без нейросети, "
            "ai — нейросеть rembg для любого фона"
        ),
    ] = "auto",
    trim: Annotated[
        bool, Field(description="Обрезать прозрачные поля по краям объекта")
    ] = False,
    pad: Annotated[int, Field(description="Отступ при trim, px", ge=0, le=500)] = 0,
    tol_bg: Annotated[
        float,
        Field(
            description="Порог «это фон», 0..255. Больше — агрессивнее режет "
            "серые/шумные края фона",
            ge=0,
            le=128,
        ),
    ] = 12.0,
    tol_fg: Annotated[
        float,
        Field(description="Порог полной непрозрачности, 0..255", ge=1, le=255),
    ] = 45.0,
    edge: Annotated[
        int, Field(description="Ширина мягкой кромки, px", ge=0, le=20)
    ] = 2,
    feather: Annotated[
        float, Field(description="Размытие альфы, sigma. 0 — резкий край", ge=0, le=10)
    ] = 0.0,
    keep_holes: Annotated[
        bool,
        Field(
            description="Оставлять белые области, не связанные с краем кадра "
            "(белые детали объекта, блики). Выключай, если нужны сквозные дырки"
        ),
    ] = True,
    ai_model: Annotated[
        str, Field(description="Для method=ai: general, u2net, human, anime, fast")
    ] = "general",
    alpha_matting: Annotated[
        bool, Field(description="Для method=ai: мягкая кромка, заметно медленнее")
    ] = False,
    suffix: Annotated[str, Field(description="Суффикс имени выходного файла")] = "_nobg",
    recursive: Annotated[
        bool, Field(description="Обходить вложенные папки")
    ] = False,
) -> str:
    """Убрать фон и сохранить PNG с прозрачностью.

    Синхронный вызов: возвращается, когда все файлы записаны на диск.
    Скорость движка white — примерно 1.5-2 с на картинку 2400x1792;
    движок ai на CPU — 5-15 с на картинку.

    Возвращает JSON: ok, total, done, failed и список results, в каждом —
    path (готовый PNG), method, width/height, bytes, opaque_share (какую долю
    кадра занял объект), notes с предупреждениями.

    Если opaque_share близка к 1 — фон не нашёлся (подними tol_bg или
    переключись на method="ai"). Если близка к 0 — вырезалось почти всё.

    Файлы с суффиксом `suffix` пропускаются, чтобы не резать свой же вывод.
    """
    result = br.remove_background_batch(
        images,
        out_dir=out_dir,
        recursive=recursive,
        method=method,
        tol_bg=tol_bg,
        tol_fg=tol_fg,
        edge=edge,
        feather=feather,
        trim=trim,
        pad=pad,
        keep_holes=keep_holes,
        ai_model=ai_model,
        alpha_matting=alpha_matting,
        suffix=suffix,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def bg_install_ai(
    gpu: Annotated[
        bool,
        Field(description="Ставить onnxruntime-gpu вместо CPU-версии (нужна CUDA)"),
    ] = False,
) -> str:
    """Доставить ИИ-движок: pip install rembg onnxruntime (~100 МБ колёс).

    Нужен только для method="ai" (не белый фон). Спрашивай пользователя перед
    вызовом — это установка пакетов в его Python. Модель (~180 МБ) скачается
    отдельно при первом запуске method="ai".
    """
    return json.dumps(br.install_ai(gpu=gpu), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
