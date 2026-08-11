#!/usr/bin/env python3
"""
MCP-сервер: локальное удаление фона, на выходе PNG с прозрачностью.

Инструменты:
  bg_check      - что доступно: библиотеки, движок ИИ, кеш моделей
  bg_remove     - убрать фон у файлов/папки/маски -> PNG с альфой
  bg_inspect    - найти непробитые белые дыры, нарисовать карту с номерами
  bg_punch      - выбить белое по точкам/номерам, которые выбрал агент глазами
  bg_shrink     - срезать N px по всей границе: убрать белую кайму
  bg_install_ai - доставить rembg+onnxruntime (нужен для method=ai)

Порядок для чистого ассета:
  bg_remove -> bg_inspect -> (агент смотрит map_png) -> bg_punch -> bg_shrink

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
import refine as rf  # noqa: E402

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
        "когда файлы уже лежат на диске. Для чистого ассета после bg_remove идёт "
        "доводка: bg_inspect (карта непробитых белых дыр) -> посмотреть карту "
        "глазами -> bg_punch по номерам дыр -> bg_shrink (срезать белую кайму)."
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
    shrink: Annotated[
        int,
        Field(
            description="Срезать N px вглубь по всей границе с прозрачностью — "
            "убирает тонкую белую обводку от неточного вырезания. 2 — норма для "
            "деталей ассета. 0 — не трогать край",
            ge=0,
            le=20,
        ),
    ] = 0,
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
        shrink=shrink,
        ai_model=ai_model,
        alpha_matting=alpha_matting,
        suffix=suffix,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def bg_inspect(
    image: Annotated[
        str, Field(description="Путь к уже вырезанному PNG с прозрачностью (после bg_remove)")
    ],
    out_dir: Annotated[
        str | None, Field(description="Куда класть карту. По умолчанию рядом с картинкой")
    ] = None,
    tol: Annotated[
        float,
        Field(
            description="Что считать белым, 0..255. 30 — норма. Подними до 45, "
            "если дыры сероватые; опусти до 18, если ловится вся светлая деталь",
            ge=0,
            le=128,
        ),
    ] = 30.0,
    min_area: Annotated[
        int, Field(description="Не показывать пятна мельче N px", ge=1, le=100000)
    ] = 40,
    max_regions: Annotated[
        int, Field(description="Сколько пятен максимум пометить", ge=1, le=300)
    ] = 60,
) -> str:
    """Найти белое, оставшееся НЕПРОЗРАЧНЫМ, и нарисовать карту с номерами.

    Зачем: bg_remove с keep_holes специально не выбивает белое внутри объекта,
    иначе выест белые клавиши и шелкографию на плате. Из-за этого настоящие
    сквозные дыры (окно экрана, отверстия в плате, просветы между рёбрами
    корпуса) остаются залитыми белым. Отличить дыру от белой детали может
    только глаз — этот инструмент готовит материал для такой проверки.

    Возвращает JSON: map_png (КАРТИНКУ НАДО ОТКРЫТЬ И ПОСМОТРЕТЬ — объект на
    тёмной клетке, каждое белое пятно обведено и пронумеровано), map_json,
    count и список regions. У каждого региона: id, seed [x,y] (точка внутри
    пятна, в пикселях оригинала), bbox, area_px, whiteness, enclosed
    (пятно со всех сторон окружено телом объекта — главный признак дыры),
    touches_frame.

    Дальше: читаешь картинку, решаешь, какие номера — настоящие дыры,
    и передаёшь их в bg_punch(ids=[...]). Белые ДЕТАЛИ не трогаешь.
    """
    return json.dumps(
        rf.inspect_image(
            image, out_dir=out_dir, tol=tol, min_area=min_area, max_regions=max_regions
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def bg_punch(
    image: Annotated[str, Field(description="Тот же PNG, что скармливал в bg_inspect")],
    ids: Annotated[
        list[int] | None,
        Field(description="Номера пятен с карты bg_inspect, которые надо выбить"),
    ] = None,
    points: Annotated[
        list[str] | None,
        Field(
            description="Свои точки в пикселях ОРИГИНАЛА в виде \"640x320\" — для дыр, "
            "которые bg_inspect не нашёл (мелкие или сероватые)"
        ),
    ] = None,
    map_json: Annotated[
        str | None,
        Field(description="Путь к <имя>_holes.json. Если не дать — ищется рядом с картинкой"),
    ] = None,
    out: Annotated[
        str | None, Field(description="Куда сохранить. По умолчанию перезаписывает исходник")
    ] = None,
    tol: Annotated[
        float, Field(description="Тот же порог белого, что был в bg_inspect", ge=0, le=128)
    ] = 30.0,
    edge: Annotated[
        int, Field(description="Мягкая кромка по краю дыры, px", ge=0, le=20)
    ] = 2,
    mode: Annotated[
        str,
        Field(
            description="flood — заливка по связной белой области под точкой (обычно). "
            "ball — снести белое в круге radius вокруг точки, если заливка протекает наружу"
        ),
    ] = "flood",
    radius: Annotated[
        int, Field(description="Радиус для mode=ball, px", ge=1, le=2000)
    ] = 24,
    shrink: Annotated[
        int, Field(description="Заодно срезать N px по границе (можно и отдельно bg_shrink)", ge=0, le=20)
    ] = 0,
) -> str:
    """Выбить белые дыры в точках, которые агент выбрал глазами по карте.

    Каждая точка/id — затравка: от неё заливкой берётся вся связная белая
    область и её альфа уходит в 0, с мягкой кромкой по краю.

    Возвращает JSON: punched (сколько точек сработало), missed, punched_px,
    opaque_before/opaque_after и по каждой точке — сработала ли и почему нет
    («под точкой не белое или уже прозрачно»).

    Если после пробивки исчезло больше, чем нужно, — область протекла наружу
    через щель в контуре: перевызови с mode="ball" и небольшим radius либо
    понизь tol.
    """
    return json.dumps(
        rf.punch_image(
            image,
            points=points,
            ids=ids,
            map_path=map_json,
            out=out,
            tol=tol,
            edge=edge,
            mode=mode,
            radius=radius,
            shrink=shrink,
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def bg_shrink(
    images: Annotated[
        list[str],
        Field(description="PNG с прозрачностью: пути, папка или маска. Можно смешивать"),
    ],
    px: Annotated[
        int,
        Field(
            description="Сколько пикселей срезать вглубь по всей границе. 2 — норма",
            ge=0,
            le=20,
        ),
    ] = 2,
    out_dir: Annotated[
        str | None, Field(description="Куда класть. По умолчанию перезаписывает исходники")
    ] = None,
    soft: Annotated[
        float, Field(description="Сглаживание новой кромки, sigma. 0 — резкий край", ge=0, le=5)
    ] = 0.5,
    min_island: Annotated[
        int, Field(description="Выкидывать оставшиеся куски мельче N px", ge=0, le=10000)
    ] = 6,
    recursive: Annotated[bool, Field(description="Обходить вложенные папки")] = False,
) -> str:
    """Срезать px пикселей вглубь везде, где непрозрачное касается прозрачного.

    Зачем: после любого вырезания по контуру остаётся тонкая светлая обводка —
    граничный пиксель наполовину фоновый, и его цвет уже не восстановить.
    Проще выбросить эти пиксели. Эрозия идёт и по внешнему контуру, и вокруг
    выбитых дыр. Пиксели, упирающиеся в рамку кадра, не срезаются.

    Делать ПОСЛЕДНИМ шагом — после bg_punch, иначе новая дыра снова принесёт
    свою кайму.

    Возвращает JSON по каждому файлу: removed_px, removed_share и warning,
    если срезано больше трети детали (значит px велик для тонкой детали).
    """
    return json.dumps(
        rf.shrink_batch(
            images,
            px=px,
            out_dir=out_dir,
            recursive=recursive,
            soft=soft,
            min_island=min_island,
        ),
        ensure_ascii=False,
        indent=2,
    )


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
