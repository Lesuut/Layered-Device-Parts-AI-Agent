#!/usr/bin/env python3
"""
MCP-сервер сборщика 2D-ассетов устройства.

Инструменты:
  asset_extract  - нарезать PNG с прозрачным фоном на детали + контактка
  asset_punch    - пробить белые внутренние области в прозрачные (вырезы)
  asset_pack     - упаковать выбранные детали в атлас + device.json
  asset_render   - отрисовать сборку (flat/exploded/grid) для проверки глазами
  asset_update   - подвинуть/отмасштабировать/повернуть детали в device.json
  asset_validate - проверить device.json на дубли, вылеты, пересечения кадров
  asset_package  - сложить готовый ассет в папку вместе с viewer.html

Запуск как MCP (stdio):
  python mcp_server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import asset_builder as ab  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]

mcp = _Server(
    "asset-builder",
    instructions=(
        "Сборка 2D-ассета устройства для игры про разборку. Конвейер: "
        "asset_extract (нарезать детали и посмотреть контактку) -> выбрать нужные "
        "глазами -> asset_punch (пробить вырезы у корпусов) -> asset_pack (атлас + "
        "device.json) -> asset_render + asset_update по кругу, пока сборка не "
        "выглядит как целое устройство -> asset_validate -> asset_package. "
        "Все инструменты синхронные, файлы на диске к моменту возврата."
    ),
)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def asset_extract(
    images: Annotated[
        list[str],
        Field(description="PNG с УЖЕ удалённым фоном (после bg_remove): пути, папка или маска"),
    ],
    out_dir: Annotated[str, Field(description="Куда сложить нарезанные детали")],
    alpha_thr: Annotated[int, Field(description="Порог альфы, 0-255", ge=1, le=254)] = 40,
    min_area_frac: Annotated[
        float, Field(description="Мин. площадь детали как доля кадра", ge=0.0, le=0.5)
    ] = 0.0004,
    min_side: Annotated[int, Field(description="Мин. сторона детали, px", ge=2, le=500)] = 14,
    close_gaps: Annotated[
        int, Field(description="Склейка разрывов, px: спасает тонкие шлейфы", ge=0, le=10)
    ] = 2,
) -> str:
    """Нарезать картинку с деталями на отдельные PNG и собрать контактку.

    Деталь = связная область непрозрачных пикселей. Возвращает список деталей
    с индексами, размерами и путь к contact_sheet.png — ОБЯЗАТЕЛЬНО прочитай
    контактку глазами (Read), на ней каждая деталь подписана номером #N.
    По этим номерам выбирай, что пойдёт в ассет, а что мусор (обрывки текста,
    тени, дубли, куски рамки).

    Если деталей вышло подозрительно мало — фон, скорее всего, не удалён:
    сначала прогони bg_remove с method="white".
    """
    return _dump(
        ab.extract_parts(images, out_dir, alpha_thr, min_area_frac, min_side,
                         close_gaps=close_gaps)
    )


@mcp.tool()
def asset_punch(
    files: Annotated[list[str], Field(description="PNG деталей, где нужны сквозные отверстия")],
    tol: Annotated[
        float, Field(description="Порог белого, 0-255. Больше — агрессивнее", ge=0, le=200)
    ] = 42.0,
    min_hole_px: Annotated[int, Field(description="Мин. площадь отверстия, px", ge=1)] = 24,
    suffix: Annotated[
        str, Field(description="Суффикс нового файла. Пусто = переписать на месте")
    ] = "",
) -> str:
    """Пробить вырезы: белое ВНУТРИ детали становится прозрачным.

    После удаления фона вырез под экран, дырки под клавиши и отверстия винтов
    остаются белыми (их специально сохраняет keep_holes). Для игрового ассета
    сквозь них должен просвечивать нижний слой — этот инструмент их пробивает.

    Применяй ВЫБОРОЧНО, только к корпусам, рамкам и передним панелям.
    НЕ применяй к светлым деталям (белая мембрана клавиатуры, наклейка
    батареи, серебристые экраны) — у них светлое это сама деталь.
    После вызова посмотри деталь глазами: не съело ли лишнего
    (поле punched_share подскажет, какая доля площади ушла).
    """
    return _dump(ab.punch_holes(files, tol, min_hole_px, suffix=suffix))


@mcp.tool()
def asset_pack(
    parts: Annotated[
        list[dict],
        Field(
            description='Детали в порядке слоёв, снизу вверх. Каждая: '
            '{"file":"...png","id":"back_cover","name":"Back Cover","layer":0,'
            '"position":[x,y],"size":[w,h],"rotation":0}. Обязателен только file; '
            'layer по умолчанию = позиция в списке, position = центр холста, '
            'size = натуральный размер детали'
        ),
    ],
    out_dir: Annotated[str, Field(description="Папка для texture.png и device.json")],
    device: Annotated[str, Field(description="Идентификатор устройства, напр. nokia_3310")] = "device",
    padding: Annotated[int, Field(description="Зазор между деталями в атласе, px", ge=0, le=64)] = 6,
    canvas: Annotated[
        list[int] | None,
        Field(description="Холст сборки [W,H]. Без него считается от самой крупной детали"),
    ] = None,
) -> str:
    """Упаковать выбранные детали в одну текстуру и создать device.json.

    Кадры в атласе гарантированно НЕ пересекаются (полочная упаковка с зазором).
    В device.json у каждой детали: frame (x,y,w,h), corners (4 угла), uv,
    position (центр на холсте), size, scale, rotation, layer.

    Порядок layer = порядок сборки снизу вверх: 0 — самая нижняя деталь
    (обычно задняя крышка), последняя — верхняя (передняя панель).
    """
    return _dump(ab.pack_atlas(parts, out_dir, device, padding, canvas=canvas))


@mcp.tool()
def asset_render(
    json_path: Annotated[str, Field(description="Путь к device.json")],
    mode: Annotated[
        str,
        Field(description="flat — собранное устройство; exploded — разнос по слоям; "
              "grid — каждая деталь отдельно с подписью"),
    ] = "flat",
    out: Annotated[str | None, Field(description="Куда сохранить PNG превью")] = None,
    background: Annotated[
        str, Field(description="checker | white | black | transparent")
    ] = "checker",
    labels: Annotated[bool, Field(description="Подписать детали прямо на превью")] = False,
) -> str:
    """Отрисовать сборку в PNG, чтобы проверить её глазами.

    ОБЯЗАТЕЛЬНО открой полученный preview через Read и посмотри:
    - деталь не торчит из корпуса, не висит в стороне, не перекрывает лишнее;
    - клавиши попадают в отверстия, экран в вырез, батарея в отсек;
    - порядок слоёв правильный (нижние детали не поверх верхних).
    Что не так — правь через asset_update и рендери снова. Обычно нужно
    2-4 итерации.
    """
    return _dump(ab.render_assembly(json_path, out, mode, background, labels))


@mcp.tool()
def asset_update(
    json_path: Annotated[str, Field(description="Путь к device.json")],
    updates: Annotated[
        list[dict],
        Field(
            description='Правки по id: [{"id":"battery","dx":0,"dy":-12}] или '
            '{"id":"keypad","position":[300,720],"size":[305,392],"rotation":0,'
            '"layer":5,"scale":1.0,"name":"Keypad"}. dx/dy — сдвиг относительно '
            'текущей позиции, position — абсолютный центр'
        ),
    ],
) -> str:
    """Подвинуть, отмасштабировать, повернуть детали или сменить слой.

    Передавай только те поля, что меняешь. После правок обязательно
    вызови asset_render и посмотри результат глазами.
    """
    return _dump(ab.update_parts(json_path, updates))


@mcp.tool()
def asset_validate(
    json_path: Annotated[str, Field(description="Путь к device.json")],
) -> str:
    """Проверить device.json: дубли id, несколько деталей на одном слое,
    кадры вне текстуры, пересечения кадров в атласе, детали далеко за холстом.

    Вызывать перед asset_package. Пересечение кадров — критично: игра будет
    резать по координатам и захватит соседнюю деталь.
    """
    return _dump(ab.validate_device(json_path))


@mcp.tool()
def asset_viewer(
    json_path: Annotated[str, Field(description="Путь к device.json")],
    out_html: Annotated[
        str | None, Field(description="Куда сохранить html; по умолчанию рядом с device.json")
    ] = None,
) -> str:
    """Собрать HTML-файл с ВШИТЫМ ассетом: двойной клик — и сборка на экране.

    Страница по file:// не может подтянуть соседние device.json и texture.png
    (браузер режет локальные запросы), поэтому и JSON, и текстура кладутся
    внутрь самого html в base64. Файл самодостаточный: его можно переслать
    или открыть на другой машине.

    `asset_package` делает такой файл автоматически — отдельно этот инструмент
    нужен, когда ассет поправили и надо пересобрать только просмотрщик.
    """
    return _dump(ab.build_standalone_viewer(json_path, out_html or ""))


@mcp.tool()
def asset_package(
    json_path: Annotated[str, Field(description="Путь к device.json")],
    out_dir: Annotated[str, Field(description="Папка готового ассета")],
    with_viewer: Annotated[bool, Field(description="Положить рядом viewer.html")] = True,
) -> str:
    """Сложить готовый ассет: texture.png + device.json + превью + viewer.html.

    Это финальный шаг. В ответе — пути ко всем файлам; покажи их пользователю
    и скажи, что viewer.html открывается двойным кликом, а внутрь надо
    перетащить папку ассета (или device.json + texture.png).
    """
    return _dump(ab.package_asset(json_path, out_dir, with_viewer))


if __name__ == "__main__":
    mcp.run()
