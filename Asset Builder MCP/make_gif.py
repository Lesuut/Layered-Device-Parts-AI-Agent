"""Запись GIF/PNG со сборкой из вьювера — для README и витрины ассетов.

Кадры снимаются с настоящего viewer.html в headless Chrome, а не рисуются
заново: то, что попадёт в README, обязано выглядеть ровно так же, как то, что
пользователь видит, открыв ОТКРЫТЬ_<device>.html двойным кликом.

Почему не встроенная кнопка «Анимация»: она крутит сцену на
requestAnimationFrame, и фаза кадра зависит от того, когда успел отработать
скриншот. Петля не сходится, разборка дёргается. Вместо неё страница отдаёт
`window.__capture` — поставили позу, сняли кадр, повторили.

Движение по кадру t = i / frames (петля бесшовная, конец стыкуется с началом):
    ry      = swing * sin(2*pi*t)      качание вокруг вертикали
    explode = (1 - cos(2*pi*t)) / 2    маятник разборки: собрано -> врозь -> собрано

Полный оборот на 360 не годится: детали — плоскости, на ry=90 сборка встаёт
ребром и исчезает.

Chrome берётся системный (playwright channel="chrome"), отдельные браузеры
playwright скачивать не нужно.

Примеры:
    py "Asset Builder MCP/make_gif.py" nokia_3310
    py "Asset Builder MCP/make_gif.py" --all --out docs/media
    py "Asset Builder MCP/make_gif.py" nokia_3310 --still --no-schema
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from asset_builder import build_standalone_viewer, log

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def asset_json(device: str) -> Path | None:
    """Найти device.json устройства в assets/, потом в work/."""
    for base in (ROOT / "assets", ROOT / "work"):
        candidate = base / device / "device.json"
        if candidate.is_file():
            return candidate
    direct = Path(device)
    if direct.is_file() and direct.suffix == ".json":
        return direct
    if (direct / "device.json").is_file():
        return direct / "device.json"
    return None


def all_devices() -> list[str]:
    base = ROOT / "assets"
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if (d / "device.json").is_file())


def capture_frames(page, args, schema: bool) -> list[Image.Image]:
    """Снять кадры позы за позой. Возвращает список RGB-картинок."""
    import io

    page.wait_for_function("() => window.__capture && window.__capture.ready()", timeout=20000)
    page.evaluate("() => window.__capture.chrome(false)")
    page.evaluate("on => window.__capture.schema(on)", schema)

    # Шаг слоя считаем от полного хода разборки: фиксированный шаг разносил
    # 14-слойную консоль вчетверо дальше 6-слойного телефона, и половина
    # деталей уезжала за кадр.
    info = page.evaluate("() => window.__capture.info()") or {}
    spread = args.travel / max(int(info.get("span", 1)), 1)

    # Автоподгон масштаба по самому разлетевшемуся кадру: он самый крупный,
    # влез он — влезут и остальные. Поля по осям разные: схема уводит подписи
    # вбок, там места нужно много, а сверху и снизу — только на полку выноски.
    mw = args.margin_w if args.margin_w is not None else (0.40 if schema else 0.86)
    mh = args.margin_h if args.margin_h is not None else (0.74 if schema else 0.86)
    zoom = args.zoom
    if not args.still:
        page.evaluate(
            "a => window.__capture.pose(a[0], 0, 1, a[1], a[2])", [args.rx, zoom, spread]
        )
        b = page.evaluate("() => window.__capture.bounds()")
        if b and b["w"] > 0 and b["h"] > 0:
            zoom *= min(mw / b["w"], mh / b["h"])

    shots: list[Image.Image] = []
    total = 1 if args.still else args.frames
    for i in range(total):
        t = 0.0 if args.still else i / args.frames
        ry = args.swing * math.sin(2 * math.pi * t)
        explode = args.still_explode if args.still else (1 - math.cos(2 * math.pi * t)) / 2
        # Разлетевшись, сборка выше собранной — на фиксированном масштабе
        # крайние слои уезжали за край кадра. Камера отъезжает ровно настолько,
        # насколько разошлись слои.
        z = zoom / (1 + args.dolly * explode)
        page.evaluate(
            "a => window.__capture.pose(a[0], a[1], a[2], a[3], a[4])",
            [args.rx, ry, explode, z, spread],
        )
        shots.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
    return shots


def write_gif(shots: list[Image.Image], out: Path, delay_ms: int, colors: int, dither: bool) -> None:
    """Собрать GIF с общей палитрой.

    Общая палитра важна: если квантовать каждый кадр отдельно, у соседних
    кадров разъезжаются цвета фона и по анимации идёт «кипение».

    Дизеринг по умолчанию выключен: детали нарисованы плоской заливкой, дробить
    её нечем, а шум дизера ломает межкадровое сжатие — файл толстеет вдвое.
    """
    mode = Image.FLOYDSTEINBERG if dither else Image.NONE
    # Палитру считаем по самому «разобранному» кадру: там на экране больше
    # всего разных деталей, значит палитра накроет и все остальные кадры.
    ref = shots[len(shots) // 2] if len(shots) > 1 else shots[0]
    base = ref.quantize(colors=colors, method=Image.MEDIANCUT)
    quantized = [s.quantize(palette=base, dither=mode) for s in shots]
    quantized[0].save(
        out,
        save_all=True,
        append_images=quantized[1:],
        duration=delay_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )


def render_grid(browser, devices: list[str], args) -> dict:
    """Витрина: все устройства плоским видом сверху, собранные, с подписями."""
    from asset_builder import load_font

    cell, pad, cap = args.cell, 10, 22
    shots: list[tuple[str, Image.Image]] = []
    for device in devices:
        json_path = asset_json(device)
        if not json_path:
            log(f"[!!] пропускаю {device}: нет device.json")
            continue
        tmp = Path(tempfile.mkdtemp(prefix="devgrid_")) / f"{device}.html"
        if not build_standalone_viewer(json_path, out_html=tmp).get("ok"):
            log(f"[!!] пропускаю {device}: не собрал страницу")
            continue
        page = browser.new_page(
            viewport={"width": cell, "height": cell}, device_scale_factor=2
        )
        try:
            page.goto(tmp.as_uri())
            page.wait_for_function("() => window.__capture && window.__capture.ready()", timeout=20000)
            page.evaluate("() => window.__capture.chrome(false)")
            page.evaluate("() => window.__capture.schema(false)")
            page.evaluate("() => window.__capture.pose(0, 0, 0, 1, 0)")
            import io

            img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
        finally:
            page.close()
        shots.append((device, img.resize((cell, cell), Image.LANCZOS)))
        log(f"[ok] {device}")

    if not shots:
        return {"ok": False, "message": "нечего складывать в витрину"}

    cols = args.cols
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new(
        "RGB", (cols * (cell + pad) + pad, rows * (cell + cap + pad) + pad), (20, 22, 26)
    )
    draw = ImageDraw.Draw(sheet)
    font = load_font(13)
    for i, (device, img) in enumerate(shots):
        x = pad + (i % cols) * (cell + pad)
        y = pad + (i // cols) * (cell + cap + pad)
        sheet.paste(img, (x, y))
        w = draw.textlength(device, font=font)
        draw.text((x + (cell - w) / 2, y + cell + 4), device, font=font, fill=(152, 160, 176))

    out_dir = Path(args.out) if args.out else ROOT / "docs" / "media"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "gallery.png"
    sheet.save(out, optimize=True)
    kb = out.stat().st_size / 1024
    return {"ok": True, "message": f"{out.name} — {len(shots)} устройств, {kb:.0f} КБ"}


def render_device(browser, device: str, args) -> dict:
    json_path = asset_json(device)
    if not json_path:
        return {"ok": False, "device": device, "message": f"не нашёл device.json для {device}"}

    out_dir = Path(args.out) if args.out else ROOT / "docs" / "media"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Съёмка идёт с той же самовместимой страницы, что получает пользователь:
    # file:// не даёт читать соседние файлы, ассет должен быть вшит внутрь.
    tmp = Path(tempfile.mkdtemp(prefix="devgif_")) / f"{device}.html"
    packed = build_standalone_viewer(json_path, out_html=tmp)
    if not packed.get("ok"):
        return {"ok": False, "device": device, "message": packed.get("message", "не собрал страницу")}

    page = browser.new_page(
        viewport={"width": args.width, "height": args.height},
        device_scale_factor=2,   # снимаем в 2x и ужимаем: текст выносок не мылится
    )
    try:
        page.goto(tmp.as_uri())
        shots = capture_frames(page, args, not args.no_schema)
    finally:
        page.close()

    shots = [s.resize((args.width, args.height), Image.LANCZOS) for s in shots]

    if args.still:
        out = out_dir / f"{device}.png"
        shots[0].save(out, optimize=True)
    else:
        out = out_dir / f"{device}.gif"
        write_gif(shots, out, args.delay, args.colors, args.dither)

    kb = out.stat().st_size / 1024
    return {
        "ok": True,
        "device": device,
        "file": str(out),
        "frames": len(shots),
        "kb": round(kb, 1),
        "message": f"{out.name} — {len(shots)} кадр(ов), {kb:.0f} КБ",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="GIF со сборкой устройства из viewer.html")
    ap.add_argument("devices", nargs="*", help="имена устройств из assets/ (или путь к device.json)")
    ap.add_argument("--all", action="store_true", help="все устройства из assets/")
    ap.add_argument("--out", help="куда класть (по умолчанию docs/media)")
    ap.add_argument("--frames", type=int, default=48, help="кадров в петле")
    ap.add_argument("--delay", type=int, default=80, help="мс на кадр")
    ap.add_argument("--width", type=int, default=700)
    ap.add_argument("--height", type=int, default=500)
    ap.add_argument("--rx", type=float, default=52, help="наклон камеры, градусы")
    ap.add_argument("--swing", type=float, default=32, help="размах качания вокруг вертикали")
    ap.add_argument("--zoom", type=float, default=1.15)
    ap.add_argument("--travel", type=float, default=430, help="полный ход разборки по Z, px (шаг слоя = ход / число ступеней)")
    ap.add_argument("--dolly", type=float, default=0.0,
                    help="доп. отъезд камеры на пике разборки; кадр и так подгоняется автоматически, это только на вкус")
    ap.add_argument("--still-explode", type=float, default=0.55, help="разборка на статичном кадре")
    ap.add_argument("--colors", type=int, default=96, help="цветов в палитре GIF")
    ap.add_argument("--margin-w", type=float, default=None,
                    help="доля ширины кадра под разлетевшуюся сборку (по умолчанию 0.40 со схемой, 0.86 без)")
    ap.add_argument("--margin-h", type=float, default=None,
                    help="доля высоты кадра под разлетевшуюся сборку (по умолчанию 0.74 со схемой, 0.86 без)")
    ap.add_argument("--dither", action="store_true", help="дизеринг палитры (по умолчанию нет)")
    ap.add_argument("--no-schema", action="store_true", help="без схемы типов")
    ap.add_argument("--still", action="store_true", help="один PNG вместо GIF")
    ap.add_argument("--grid", action="store_true", help="одна витрина из всех устройств вместо отдельных файлов")
    ap.add_argument("--cols", type=int, default=7, help="колонок в витрине")
    ap.add_argument("--cell", type=int, default=210, help="сторона клетки витрины, px")
    ap.add_argument("--headed", action="store_true", help="показать окно Chrome")
    args = ap.parse_args()

    devices = all_devices() if args.all else args.devices
    if not devices:
        ap.error("укажи устройство или --all")

    from playwright.sync_api import sync_playwright

    failures = 0
    with sync_playwright() as pw:
        # Один браузер на весь прогон: на --all поднимать Chrome под каждое
        # устройство — минуты чистого старта процесса.
        browser = pw.chromium.launch(channel="chrome", headless=not args.headed)
        try:
            if args.grid:
                res = render_grid(browser, devices, args)
                log(("[ok] " if res["ok"] else "[!!] ") + res["message"])
                failures += 0 if res["ok"] else 1
            else:
                for device in devices:
                    res = render_device(browser, device, args)
                    log(("[ok] " if res["ok"] else "[!!] ") + res["message"])
                    failures += 0 if res["ok"] else 1
        finally:
            browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
