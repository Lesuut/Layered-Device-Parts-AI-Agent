"""Recording a GIF/PNG of the assembly out of the viewer — for the README and the asset showcase.

The frames are shot off the real viewer.html in headless Chrome rather than
drawn again: what lands in the README has to look exactly like what the user
sees when they double-click OPEN_<device>.html.

Why not the built-in "Animation" button: it spins the scene on
requestAnimationFrame, and the phase of a frame depends on when the screenshot
managed to run. The loop does not close and the teardown judders. Instead the
page exposes `window.__capture` — set a pose, take a frame, repeat.

Motion over the frame t = i / frames (a seamless loop, the end meets the start):
    ry      = swing * sin(2*pi*t)      swinging around the vertical
    explode = (1 - cos(2*pi*t)) / 2    the teardown pendulum: together -> apart -> together

A full 360 turn is no good: the parts are planes, and at ry=90 the assembly
turns edge-on and disappears.

Chrome is taken from the system (playwright channel="chrome"), there is no need
to download playwright's own browsers.

Examples:
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
    """Find the device's device.json in assets/, then in work/."""
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
    """Shoot the frames pose by pose. Returns a list of RGB images."""
    import io

    page.wait_for_function("() => window.__capture && window.__capture.ready()", timeout=20000)
    page.evaluate("() => window.__capture.chrome(false)")
    # The callouts trail behind the parts with a delay — that is part of the
    # mechanic and it has to survive the recording. Their clock is swapped for
    # the frame step: a callout lives one pose for as long as a GIF frame lasts.
    step = 0 if args.still else args.delay
    page.evaluate("a => window.__capture.schema(a[0], a[1])", [schema, step])

    # The layer step is computed from the full teardown travel: a fixed step
    # spread a 14-layer console four times further than a 6-layer phone, and
    # half the parts drove off frame.
    info = page.evaluate("() => window.__capture.info()") or {}
    spread = args.travel / max(int(info.get("span", 1)), 1)

    # Scale is auto-fitted on the most exploded frame: it is the largest one, and
    # if it fits, the rest fit too. The margins differ per axis: the schema pushes
    # labels sideways and needs room there, while top and bottom only take the callout shelf.
    mw = args.margin_w if args.margin_w is not None else (0.40 if schema else 0.86)
    mh = args.margin_h if args.margin_h is not None else (0.74 if schema else 0.86)
    zoom = args.zoom
    if not args.still:
        # The full teardown is measured in the three extreme swing poses: a rotated
        # assembly is wider than a straight one, and fitting on ry=0 alone clipped the outer parts.
        worst_w = worst_h = 0.0
        for ry in (0.0, args.swing, -args.swing):
            page.evaluate(
                "a => window.__capture.pose(a[0], a[1], 1, a[2], a[3])",
                [args.rx, ry, zoom, spread],
            )
            b = page.evaluate("() => window.__capture.bounds()")
            if b:
                worst_w = max(worst_w, b["w"])
                worst_h = max(worst_h, b["h"])
        if worst_w > 0 and worst_h > 0:
            zoom *= min(mw / worst_w, mh / worst_h)

    def pose(i: int) -> None:
        t = 0.0 if args.still else i / args.frames
        ry = args.swing * math.sin(2 * math.pi * t)
        explode = args.still_explode if args.still else (1 - math.cos(2 * math.pi * t)) / 2
        # Once exploded, the assembly is taller than the assembled one — at a fixed
        # scale the outer layers drove off the edge of the frame. The camera pulls
        # back exactly as far as the layers moved apart.
        z = zoom / (1 + args.dolly * explode)
        page.evaluate(
            "a => window.__capture.pose(a[0], a[1], a[2], a[3], a[4])",
            [args.rx, ry, explode, z, spread],
        )

    total = 1 if args.still else args.frames

    # A warm-up lap with no capture. A callout lags behind its part, so its
    # position depends on the previous frames — shooting the loop from a cold
    # start would give a jerk at the seam. After a lap the lag settles into its
    # own cycle and the last frame meets the first.
    if not args.still:
        for i in range(total):
            pose(i)

    shots: list[Image.Image] = []
    for i in range(total):
        pose(i)
        shots.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB"))
    return shots


def write_gif(shots: list[Image.Image], out: Path, delay_ms: int, colors: int, dither: bool) -> None:
    """Build a GIF with a shared palette.

    The shared palette matters: quantising every frame separately makes the
    background colours drift between neighbouring frames and the animation "boils".

    Dithering is off by default: the parts are drawn with flat fills, there is
    nothing to break up, and dither noise ruins inter-frame compression — the file doubles in size.
    """
    mode = Image.FLOYDSTEINBERG if dither else Image.NONE
    # The palette is computed on the most "taken apart" frame: that is where the
    # screen holds the most different parts, so the palette covers every other frame too.
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
    """Showcase: every device flat top-down, assembled, with labels."""
    from asset_builder import load_font

    cell, pad, cap = args.cell, 10, 22
    shots: list[tuple[str, Image.Image]] = []
    for device in devices:
        json_path = asset_json(device)
        if not json_path:
            log(f"[!!] skipping {device}: no device.json")
            continue
        tmp = Path(tempfile.mkdtemp(prefix="devgrid_")) / f"{device}.html"
        if not build_standalone_viewer(json_path, out_html=tmp).get("ok"):
            log(f"[!!] skipping {device}: could not build the page")
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
        return {"ok": False, "message": "nothing to put in the showcase"}

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
    return {"ok": True, "message": f"{out.name} — {len(shots)} devices, {kb:.0f} KB"}


def render_device(browser, device: str, args) -> dict:
    json_path = asset_json(device)
    if not json_path:
        return {"ok": False, "device": device, "message": f"could not find device.json for {device}"}

    out_dir = Path(args.out) if args.out else ROOT / "docs" / "media"
    out_dir.mkdir(parents=True, exist_ok=True)

    # The shooting runs off the same self-contained page the user gets:
    # file:// does not allow reading neighbouring files, the asset must be baked in.
    tmp = Path(tempfile.mkdtemp(prefix="devgif_")) / f"{device}.html"
    packed = build_standalone_viewer(json_path, out_html=tmp)
    if not packed.get("ok"):
        return {"ok": False, "device": device, "message": packed.get("message", "could not build the page")}

    page = browser.new_page(
        viewport={"width": args.width, "height": args.height},
        device_scale_factor=2,   # shoot at 2x and shrink: callout text stays sharp
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
        "message": f"{out.name} — {len(shots)} frame(s), {kb:.0f} KB",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="GIF of a device assembly from viewer.html")
    ap.add_argument("devices", nargs="*", help="device names from assets/ (or a path to device.json)")
    ap.add_argument("--all", action="store_true", help="every device in assets/")
    ap.add_argument("--out", help="where to put it (docs/media by default)")
    ap.add_argument("--frames", type=int, default=48, help="frames in the loop")
    ap.add_argument("--delay", type=int, default=80, help="ms per frame")
    ap.add_argument("--width", type=int, default=700)
    ap.add_argument("--height", type=int, default=500)
    ap.add_argument("--rx", type=float, default=52, help="camera tilt, degrees")
    ap.add_argument("--swing", type=float, default=32, help="swing amplitude around the vertical")
    ap.add_argument("--zoom", type=float, default=1.15)
    ap.add_argument("--travel", type=float, default=430, help="full teardown travel along Z, px (layer step = travel / number of steps)")
    ap.add_argument("--dolly", type=float, default=0.0,
                    help="extra camera pull-back at the peak of the teardown; the frame is auto-fitted anyway, this is taste only")
    ap.add_argument("--still-explode", type=float, default=0.55, help="teardown amount on the still frame")
    ap.add_argument("--colors", type=int, default=96, help="colours in the GIF palette")
    ap.add_argument("--margin-w", type=float, default=None,
                    help="share of the frame width for the exploded assembly (0.40 with the schema, 0.86 without)")
    ap.add_argument("--margin-h", type=float, default=None,
                    help="share of the frame height for the exploded assembly (0.74 with the schema, 0.86 without)")
    ap.add_argument("--dither", action="store_true", help="palette dithering (off by default)")
    ap.add_argument("--no-schema", action="store_true", help="without the type schema")
    ap.add_argument("--still", action="store_true", help="one PNG instead of a GIF")
    ap.add_argument("--grid", action="store_true", help="one showcase of every device instead of separate files")
    ap.add_argument("--cols", type=int, default=7, help="columns in the showcase")
    ap.add_argument("--cell", type=int, default=210, help="showcase cell side, px")
    ap.add_argument("--headed", action="store_true", help="show the Chrome window")
    args = ap.parse_args()

    devices = all_devices() if args.all else args.devices
    if not devices:
        ap.error("name a device or pass --all")

    from playwright.sync_api import sync_playwright

    failures = 0
    with sync_playwright() as pw:
        # One browser for the whole run: on --all, bringing Chrome up per device
        # costs minutes of pure process start.
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
