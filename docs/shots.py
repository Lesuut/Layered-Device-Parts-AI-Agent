"""Скриншоты интерфейсов для README: вьювер целиком и панель прогресса.

Отдельно от make_gif.py: там снимается сцена со сборкой без обвязки, тут
наоборот — нужна вся обвязка, кнопки и список деталей, ради этого всё и есть.

    py docs/shots.py                       вьювер (nokia_3310) + панель
    py docs/shots.py --device gameboy_advance
    py docs/shots.py --skip-dashboard

Панель снимается с живого сервера: подними её заранее
`py "Pipeline Dashboard/server.py" --ensure`, иначе шаг пропустится.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "Asset Builder MCP"))

from PIL import Image  # noqa: E402

from asset_builder import build_standalone_viewer, log  # noqa: E402

MEDIA = ROOT / "docs" / "media"
DASHBOARD_URL = "http://127.0.0.1:7788/"


def shoot_viewer(browser, device: str, width: int, height: int) -> None:
    """Вьювер как его видит человек: шапка, сцена, схема, список деталей."""
    json_path = ROOT / "assets" / device / "device.json"
    if not json_path.is_file():
        log(f"[!!] нет {json_path}")
        return
    tmp = Path(tempfile.mkdtemp(prefix="devshot_")) / f"{device}.html"
    if not build_standalone_viewer(json_path, out_html=tmp).get("ok"):
        log("[!!] не собрал страницу вьювера")
        return

    page = browser.new_page(
        viewport={"width": width, "height": height}, device_scale_factor=2
    )
    try:
        page.goto(tmp.as_uri())
        page.wait_for_function("() => window.__capture && window.__capture.ready()", timeout=20000)
        page.check("#schemaChk")
        page.click("#toggleList")          # правая колонка со списком деталей
        page.evaluate("() => window.__capture.pose(46, -22, 0.45, 1.05, 62)")
        page.wait_for_timeout(700)         # выноски доезжают до своих мест
        img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    finally:
        page.close()

    out = MEDIA / "viewer.png"
    img.resize((width, height), Image.LANCZOS).save(out, optimize=True)
    log(f"[ok] {out.name} — {out.stat().st_size / 1024:.0f} КБ")


def shoot_dashboard(browser, width: int, height: int) -> None:
    try:
        urllib.request.urlopen(DASHBOARD_URL, timeout=2).read(1)
    except Exception as exc:
        log(f"[..] панель не отвечает ({exc}), пропускаю — подними её и запусти снова")
        return

    page = browser.new_page(
        viewport={"width": width, "height": height}, device_scale_factor=2
    )
    try:
        page.goto(DASHBOARD_URL)
        page.wait_for_timeout(2500)        # страница опрашивает /api/state раз в 700 мс
        img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    finally:
        page.close()

    out = MEDIA / "dashboard.png"
    img.resize((width, height), Image.LANCZOS).save(out, optimize=True)
    log(f"[ok] {out.name} — {out.stat().st_size / 1024:.0f} КБ")


def shoot_atlas(device: str, width: int) -> None:
    """Атлас деталей на непрозрачном фоне.

    texture.png лежит в репозитории, но он прозрачный: на тёмной теме GitHub
    тёмный корпус на нём пропадает. Для README нужна подложка.
    """
    tex = ROOT / "assets" / device / "texture.png"
    if not tex.is_file():
        log(f"[!!] нет {tex}")
        return
    src = Image.open(tex).convert("RGBA")
    scale = width / src.width
    src = src.resize((width, max(1, round(src.height * scale))), Image.LANCZOS)
    plate = Image.new("RGB", src.size, (20, 22, 26))
    plate.paste(src, (0, 0), src)
    out = MEDIA / "atlas.png"
    plate.save(out, optimize=True)
    log(f"[ok] {out.name} — {out.stat().st_size / 1024:.0f} КБ")


def main() -> int:
    ap = argparse.ArgumentParser(description="скриншоты интерфейсов для README")
    ap.add_argument("--device", default="nokia_3310")
    ap.add_argument("--width", type=int, default=1180)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--atlas-width", type=int, default=760)
    ap.add_argument("--skip-viewer", action="store_true")
    ap.add_argument("--skip-atlas", action="store_true")
    ap.add_argument("--skip-dashboard", action="store_true")
    args = ap.parse_args()

    MEDIA.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        try:
            if not args.skip_viewer:
                shoot_viewer(browser, args.device, args.width, args.height)
            if not args.skip_dashboard:
                shoot_dashboard(browser, args.width, args.height)
            if not args.skip_atlas:
                shoot_atlas(args.device, args.atlas_width)
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
