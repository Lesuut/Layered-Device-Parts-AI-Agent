#!/usr/bin/env python3
"""
MCP-сервер над Google Flow: генерация картинок в 2K из уже открытого Chrome.

Инструменты:
  flow_check           - проверить готовность (порт отладки, вкладка Flow, логин)
  flow_setup           - поднять отладочный Chrome, если он не запущен
  flow_generate        - промт (+референсы) -> генерация -> ДОЖДАТЬСЯ -> скачать 2K
  flow_paste_reference - только вставить референс в композер, без генерации
  flow_list_files      - показать, что уже нагенерировано в папке вывода

Запуск как MCP (stdio):
  python mcp_server.py

Регистрация в Claude Code:
  claude mcp add flow-images -- python "<путь>/mcp_server.py"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flow_gen as fg  # noqa: E402
import run as launcher  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]

DEFAULT_OUT = HERE / "output"

mcp = _Server(
    "flow-images",
    instructions=(
        "Генерация изображений в Google Flow (labs.google) через уже открытый Chrome. "
        "Перед первой генерацией вызови flow_check; если не готово — flow_setup. "
        "flow_generate блокируется до конца: возвращает управление только когда все "
        "картинки (по умолчанию 4) сгенерированы и скачаны в 2K на диск. "
        "Есть работа по референсу: передай reference_images в flow_generate — "
        "картинки вставятся в композер, дождутся загрузки, и только потом "
        "запустится генерация."
    ),
)


@mcp.tool()
def flow_check() -> str:
    """Проверить готовность окружения Flow.

    Смотрит: запущен ли Chrome с портом отладки 9222, открыта ли вкладка проекта
    Flow, не висит ли страница входа Google. Вызывать перед flow_generate.
    """
    return json.dumps(fg.check_environment(), ensure_ascii=False, indent=2)


@mcp.tool()
def flow_setup() -> str:
    """Поднять отладочный Chrome для автоматизации Flow, если он ещё не запущен.

    Обычный Chrome пользователя закрывать не нужно — запускается отдельный процесс
    с профилем ChromeDebugProfile. Если профиль новый, пользователю нужно вручную
    войти в Google и открыть проект Flow.
    """
    info = launcher.cdp_version()
    if info:
        return json.dumps(
            {"started": False, "already_running": True, "browser": info.get("Browser")},
            ensure_ascii=False,
        )
    launcher.launch_chrome()
    launcher.wait_for_port(45)
    return json.dumps(
        {
            "started": True,
            "next_step": "Если вкладки Flow нет — войти в Google и открыть проект, "
            "затем вызвать flow_check",
        },
        ensure_ascii=False,
    )


@mcp.tool()
def flow_generate(
    prompt: Annotated[str, Field(description="Текст промта для генерации")],
    count: Annotated[int, Field(description="Сколько картинок ждать", ge=1, le=8)] = 4,
    out_dir: Annotated[
        str | None, Field(description="Куда сохранить; по умолчанию папка output рядом с сервером")
    ] = None,
    quality: Annotated[
        str, Field(description="2k (по умолчанию, 2400x1792), 1k, 4k (нужен тариф), src")
    ] = "2k",
    gen_timeout: Annotated[int, Field(description="Ожидание генерации, сек")] = 600,
    ui_timeout: Annotated[int, Field(description="Ожидание апскейла и загрузки, сек")] = 180,
    reference_images: Annotated[
        list[str] | None,
        Field(
            description="Картинки-референсы: абсолютные пути к файлам, data:-URI "
            "или http(s)-ссылки. Вставляются в композер Flow (как Ctrl+V) ДО "
            "генерации; кнопка жмётся только после того, как они догрузились"
        ),
    ] = None,
    ref_method: Annotated[
        str,
        Field(
            description="Способ вставки: auto (перебор, по умолчанию), paste, "
            "clipboard (настоящий Ctrl+V через буфер Windows), drop, upload"
        ),
    ] = "auto",
    ref_timeout: Annotated[int, Field(description="Ожидание загрузки референса, сек")] = 90,
) -> str:
    """Сгенерировать картинки в Google Flow и скачать их на диск.

    ВАЖНО: вызов блокирующий и возвращается ТОЛЬКО после того, как все `count`
    картинок сгенерированы и файлы сохранены на диск. Типичное время — 1.5-3 минуты
    на 4 картинки в 2K. Не вызывай повторно, пока предыдущий вызов не вернулся.

    Референсы (`reference_images`) — картинки-образцы: они вставляются в поле
    ввода Flow, скрипт ждёт окончания их загрузки на сайт и только потом жмёт
    «генерировать». Если референс вставить не удалось, генерация НЕ запускается
    и возвращается error=reference_upload_failed (квота не тратится).

    Возвращает JSON: ok, downloaded, folder, elapsed_sec, references и список
    images с полями path, filename, width, height, bytes, upscaled_2k.
    Если ok=false — смотри поля error и message.
    """
    result = fg.generate_images(
        prompt=prompt,
        count=count,
        out_dir=out_dir or str(DEFAULT_OUT),
        quality=quality,
        gen_timeout=gen_timeout,
        ui_timeout=ui_timeout,
        references=reference_images,
        ref_method=ref_method,
        ref_timeout=ref_timeout,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def flow_paste_reference(
    reference_images: Annotated[
        list[str],
        Field(description="Пути к файлам, data:-URI или ссылки на картинки-референсы"),
    ],
    ref_method: Annotated[
        str, Field(description="auto | paste | clipboard | drop | upload")
    ] = "auto",
    ref_timeout: Annotated[int, Field(description="Ожидание загрузки, сек")] = 90,
) -> str:
    """Вставить референсы в композер Flow БЕЗ генерации.

    Нужно, чтобы проверить, что вставка работает, не тратя квоту генераций,
    либо чтобы подготовить композер, а генерацию запустить отдельно.
    Возвращает JSON с ok и списком references (метод вставки, ошибки).
    """
    return json.dumps(
        fg.paste_references(
            references=reference_images, ref_method=ref_method, ref_timeout=ref_timeout
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def flow_list_files(
    out_dir: Annotated[str | None, Field(description="Папка вывода")] = None,
    limit: Annotated[int, Field(description="Сколько последних папок показать", ge=1, le=50)] = 10,
) -> str:
    """Показать последние сгенерированные партии картинок и пути к файлам."""
    root = Path(out_dir) if out_dir else DEFAULT_OUT
    if not root.exists():
        return json.dumps({"folder": str(root), "batches": []}, ensure_ascii=False)

    batches = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
        files = sorted(d.glob("*.*"))
        batches.append(
            {
                "batch": d.name,
                "path": str(d),
                "count": len(files),
                "files": [
                    {
                        "path": str(f),
                        "bytes": f.stat().st_size,
                        "size": (lambda s: f"{s[0]}x{s[1]}" if s else None)(fg.image_size(f)),
                    }
                    for f in files
                ],
            }
        )
    return json.dumps({"folder": str(root), "batches": batches}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
