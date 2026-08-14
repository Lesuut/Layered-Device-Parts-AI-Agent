#!/usr/bin/env python3
"""Finishing a cut-out PNG: unpunched white holes and the fringe along the contour.

Why. The `white` engine deliberately leaves white INSIDE the object (keep_holes):
otherwise it would eat the white keys, the silkscreen on the board and the
highlights. The price is that genuine see-through holes (the screen window, holes
in the board, gaps between the shell ribs) stay filled with white. Algorithmically
a "hole" and a "white part" are indistinguishable — the eye decides.

Hence a pipeline of three steps:

  1. inspect_image  — finds EVERY white blob that stayed opaque and draws a map
                      with numbered marks (PNG) plus a machine-readable list
                      (JSON). The agent looks at the map with its eyes.
  2. punch_image    — the agent hands over the mark numbers and/or its own points;
                      each point flood-fills its connected white area -> alpha 0.
  3. shrink_image   — along every boundary with transparency it cuts N pixels
                      inward: removes the thin white outline that an imprecise
                      cutout always leaves (bg_remove.shrink_alpha).

Standalone usage:
  python refine.py inspect  cut.png
  python refine.py punch    cut.png --ids 1,4,7 --points 512x300,900x412
  python refine.py shrink   cut.png --px 2
  python refine.py polish   cut.png --map cut_holes.json --ids 1,4 --px 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

import bg_remove as br

Image.MAX_IMAGE_PIXELS = None

# The hole map is drawn on a dark background: a white blob on a white sheet is invisible.
MAP_BG = (18, 20, 28)
MAP_BG_ALT = (27, 31, 43)
MARK_FILL = (255, 40, 90)
MARK_LINE = (255, 214, 0)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def load_rgba(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """RGB float32 0..255 and alpha 0..1. For a picture with no alpha it is all ones."""
    rgb, alpha = br.load_rgb(Path(path))
    if alpha is None:
        alpha = np.ones(rgb.shape[:2], dtype=np.float32)
    return rgb, alpha.astype(np.float32)


def _font(size: int):
    for name in ("arialbd.ttf", "Arial Bold.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def map_paths_for(src: Path, out_dir: str | Path | None) -> tuple[Path, Path]:
    root = Path(out_dir) if out_dir else src.parent
    return root / f"{src.stem}_holes.png", root / f"{src.stem}_holes.json"


# ---------------------------------------------------------------------------
# Step 1: find the white blobs that stayed opaque
# ---------------------------------------------------------------------------


def find_white_regions(
    rgb: np.ndarray,
    alpha: np.ndarray,
    tol: float = 30.0,
    min_area: int = 40,
    opaque_thr: float = 0.5,
    max_regions: int = 60,
) -> list[dict]:
    """Connected areas of "almost white and opaque at the same time".

    tol — how far from pure white still counts as white (0..255).
    Returns a list sorted by area: the most noticeable blobs first.

    Every area's seed is not the centre of mass (for a horseshoe that is outside)
    but the "deepest" point by the distance transform: it is guaranteed to be
    inside and works as a seed for the fill at the punch step.
    """
    dist = br.whiteness_distance(rgb)
    mask = (dist <= float(tol)) & (alpha >= opaque_thr)
    if not mask.any():
        return []

    labels, n = ndimage.label(mask)
    if not n:
        return []

    sizes = np.bincount(labels.ravel())
    order = [i for i in np.argsort(sizes[1:])[::-1] + 1 if sizes[i] >= int(min_area)]
    order = order[: int(max_regions)]

    boxes = ndimage.find_objects(labels)
    solid = alpha >= opaque_thr
    h, w = mask.shape
    out: list[dict] = []

    for rank, lab in enumerate(order, 1):
        sy, sx = boxes[lab - 1]
        y0, y1, x0, x1 = sy.start, sy.stop, sx.start, sx.stop
        # We work in a cutout with a 1 px margin: an EDT and a neighbourhood check on
        # the full frame for fifty-odd areas costs seconds for no reason.
        py0, py1 = max(y0 - 1, 0), min(y1 + 1, h)
        px0, px1 = max(x0 - 1, 0), min(x1 + 1, w)
        crop = labels[py0:py1, px0:px1] == lab

        edt = ndimage.distance_transform_edt(crop)
        cy, cx = np.unravel_index(int(np.argmax(edt)), edt.shape)
        depth = float(edt[cy, cx])

        ring = ndimage.binary_dilation(crop, structure=np.ones((3, 3))) & ~crop
        ring_solid = solid[py0:py1, px0:px1][ring]
        enclosed = bool(ring_solid.size) and bool(ring_solid.all())

        area = int(sizes[lab])
        bbox_area = (y1 - y0) * (x1 - x0)
        region_rgb = rgb[py0:py1, px0:px1][crop]

        out.append(
            {
                "id": rank,
                "seed": [int(px0 + cx), int(py0 + cy)],
                "bbox": [int(x0), int(y0), int(x1), int(y1)],
                "area_px": area,
                "area_share": round(area / float(h * w), 5),
                "depth_px": round(depth, 1),
                "fill_ratio": round(area / bbox_area, 3) if bbox_area else 0.0,
                # enclosed = the blob is surrounded by the body of the object on every side.
                # Those are the prime candidates for see-through holes.
                "enclosed": enclosed,
                "touches_frame": bool(x0 == 0 or y0 == 0 or x1 == w or y1 == h),
                "mean_rgb": [int(v) for v in region_rgb.mean(axis=0)],
                "whiteness": round(float(dist[py0:py1, px0:px1][crop].mean()), 1),
            }
        )

    return out


def render_hole_map(
    rgb: np.ndarray,
    alpha: np.ndarray,
    regions: list[dict],
    out_path: Path,
    max_side: int = 1600,
    checker: int = 40,
) -> dict:
    """A picture for the agent's eyes: the object on a dark checker, blobs marked.

    Returns {path, scale, width, height}. scale — how many times smaller the map is
    than the original; the coordinates in the JSON are always in pixels of the
    ORIGINAL, so punch does not have to convert anything.
    """
    h, w = alpha.shape
    scale = min(1.0, float(max_side) / max(h, w))
    tw, th = max(int(w * scale), 1), max(int(h * scale), 1)

    yy, xx = np.mgrid[0:th, 0:tw]
    board = np.where(
        (((yy // checker) + (xx // checker)) % 2)[..., None],
        np.array(MAP_BG_ALT, dtype=np.float32),
        np.array(MAP_BG, dtype=np.float32),
    )

    rgba = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha * 255.0, 0, 255)]).astype(np.uint8)
    small = np.asarray(
        Image.fromarray(rgba, "RGBA").resize((tw, th), Image.LANCZOS), dtype=np.float32
    )
    a = small[..., 3:4] / 255.0
    flat = Image.fromarray((small[..., :3] * a + board * (1 - a)).astype(np.uint8), "RGB")

    draw = ImageDraw.Draw(flat, "RGBA")
    font = _font(max(14, int(min(tw, th) / 40)))

    for r in regions:
        x0, y0, x1, y1 = [int(v * scale) for v in r["bbox"]]
        draw.rectangle([x0, y0, max(x1 - 1, x0), max(y1 - 1, y0)], fill=MARK_FILL + (70,))
        draw.rectangle(
            [x0, y0, max(x1 - 1, x0), max(y1 - 1, y0)], outline=MARK_LINE + (255,), width=2
        )
        sx, sy = int(r["seed"][0] * scale), int(r["seed"][1] * scale)
        draw.line([(sx - 7, sy), (sx + 7, sy)], fill=MARK_LINE + (255,), width=2)
        draw.line([(sx, sy - 7), (sx, sy + 7)], fill=MARK_LINE + (255,), width=2)

        text = str(r["id"])
        tb = draw.textbbox((0, 0), text, font=font)
        pad = 5
        bw, bh = tb[2] - tb[0] + pad * 2, tb[3] - tb[1] + pad * 2
        # The label is kept inside the frame, otherwise the number drifts off for edge blobs.
        bx = min(max(sx + 9, 0), tw - bw)
        by = min(max(sy - bh - 4, 0), th - bh)
        draw.rectangle([bx, by, bx + bw, by + bh], fill=(0, 0, 0, 205))
        draw.text((bx + pad - tb[0], by + pad - tb[1]), text, font=font, fill=MARK_LINE + (255,))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    flat.save(out_path, "PNG", compress_level=6)
    return {"path": str(out_path), "scale": round(scale, 4), "width": tw, "height": th}


def inspect_image(
    image: str | Path,
    out_dir: str | Path | None = None,
    tol: float = 30.0,
    min_area: int = 40,
    max_regions: int = 60,
    max_side: int = 1600,
) -> dict:
    """Find the unpunched white blobs and return a map for a visual check.

    Writes two files next to it: <name>_holes.png (to look at) and
    <name>_holes.json (to feed into punch_image through map=).
    """
    started = time.time()
    src = Path(image)
    if not src.is_file():
        return {"ok": False, "error": "not_found", "message": f"No file: {src}"}

    rgb, alpha = load_rgba(src)
    if float((alpha < 0.5).mean()) < 0.001:
        return {
            "ok": False,
            "error": "no_alpha",
            "message": "The picture has no transparent pixels — run bg_remove first",
            "src": str(src),
        }

    regions = find_white_regions(rgb, alpha, tol=tol, min_area=min_area, max_regions=max_regions)
    map_png, map_json = map_paths_for(src, out_dir)
    preview = render_hole_map(rgb, alpha, regions, map_png, max_side=max_side)

    data = {
        "ok": True,
        "src": str(src),
        "image_size": [int(alpha.shape[1]), int(alpha.shape[0])],
        "tol": tol,
        "map_png": preview["path"],
        "map_scale": preview["scale"],
        "map_json": str(map_json),
        "count": len(regions),
        "enclosed_count": sum(1 for r in regions if r["enclosed"]),
        "regions": regions,
        "elapsed_sec": round(time.time() - started, 2),
        "hint": (
            "Open map_png. The numbered blobs are white that stayed opaque. "
            "Hand the real holes (the screen window, board holes, shell gaps) to "
            "punch by id; do not touch white PARTS (keys, silkscreen, a highlight)."
        ),
    }
    map_json.parent.mkdir(parents=True, exist_ok=True)
    map_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# Step 2: punch the holes by the marks
# ---------------------------------------------------------------------------


def _seed_list(
    points: list | None,
    ids: list[int] | None,
    map_data: dict | None,
) -> tuple[list[tuple[int, int]], list[str]]:
    """Reduce the agent's points and the map ids to a list of seeds (x, y)."""
    seeds: list[tuple[int, int]] = []
    notes: list[str] = []

    for p in points or []:
        if isinstance(p, dict):
            x, y = p.get("x"), p.get("y")
        elif isinstance(p, str):
            sep = "x" if "x" in p.lower() else ","
            parts = p.lower().replace(";", ",").split(sep)
            x, y = (parts + [None, None])[:2]
        else:
            x, y = (list(p) + [None, None])[:2]
        try:
            seeds.append((int(float(x)), int(float(y))))
        except (TypeError, ValueError):
            notes.append(f"could not parse point {p!r} — skipped")

    if ids:
        by_id = {r["id"]: r for r in (map_data or {}).get("regions", [])}
        for i in ids:
            r = by_id.get(int(i))
            if r:
                seeds.append(tuple(r["seed"]))
            else:
                notes.append(f"id {i} is not on the map — skipped")

    return seeds, notes


def punch_regions(
    rgb: np.ndarray,
    alpha: np.ndarray,
    seeds: list[tuple[int, int]],
    tol: float = 30.0,
    edge: int = 2,
    tol_fg: float = 45.0,
    mode: str = "flood",
    radius: int = 24,
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Knock out white at the seed points. Returns (rgb, alpha, a per-point report).

    mode="flood" — fill the connected white area under the point (the usual case).
    mode="ball"  — wipe white inside a circle of radius around the point; it helps
                   when the area leaks outward through a gap in the contour.
    """
    dist = br.whiteness_distance(rgb)
    white = (dist <= float(tol)) & (alpha >= 0.5)
    labels, n = ndimage.label(white)
    h, w = alpha.shape

    hit = np.zeros_like(white)
    report: list[dict] = []

    for x, y in seeds:
        if not (0 <= x < w and 0 <= y < h):
            report.append({"seed": [x, y], "ok": False, "why": "the point is outside the frame"})
            continue

        if mode == "ball":
            r = int(radius)
            yy, xx = np.ogrid[0:h, 0:w]
            ball = ((xx - x) ** 2 + (yy - y) ** 2) <= r * r
            area = ball & white
        else:
            lab = int(labels[y, x])
            if not lab:
                report.append(
                    {
                        "seed": [x, y],
                        "ok": False,
                        "why": "not white under the point, or already transparent "
                        f"(distance to white {dist[y, x]:.0f}, alpha {alpha[y, x]:.2f})",
                    }
                )
                continue
            area = labels == lab

        painted = int((area & ~hit).sum())
        hit |= area
        report.append({"seed": [x, y], "ok": painted > 0, "punched_px": painted, "mode": mode})

    if not hit.any():
        return rgb, alpha, report

    out = alpha.copy()
    if edge > 0:
        # The same soft edge as in the main cutout: a hard threshold cut gives
        # jagged teeth on rounded holes.
        band = ndimage.binary_dilation(hit, iterations=int(edge)) & ~hit
        soft = np.clip((dist - tol) / max(tol_fg - tol, 1e-6), 0.0, 1.0)
        out[band] = np.minimum(out[band], soft[band])
    out[hit] = 0.0

    return br.defringe(rgb, out), out, report


def punch_image(
    image: str | Path,
    points: list | None = None,
    ids: list[int] | None = None,
    map_path: str | Path | None = None,
    out: str | Path | None = None,
    tol: float = 30.0,
    edge: int = 2,
    mode: str = "flood",
    radius: int = 24,
    shrink: int = 0,
    compress: int = 6,
) -> dict:
    """Punch the white holes by the agent's marks. Without `out` it overwrites the source.

    points — [[x, y], ...] or [{"x":..,"y":..}] in pixels of the ORIGINAL;
    ids    — blob numbers from the inspect_image map (map_path is needed).
    """
    started = time.time()
    src = Path(image)
    if not src.is_file():
        return {"ok": False, "error": "not_found", "message": f"No file: {src}"}

    map_data = None
    if ids and not map_path:
        guess = map_paths_for(src, None)[1]
        map_path = guess if guess.is_file() else None
    if map_path:
        try:
            map_data = json.loads(Path(map_path).read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "bad_map", "message": str(exc)}

    seeds, notes = _seed_list(points, ids, map_data)
    if not seeds:
        return {
            "ok": False,
            "error": "no_seeds",
            "message": "Not a single point/id was passed",
            "notes": notes,
        }

    rgb, alpha = load_rgba(src)
    before = float((alpha >= 0.5).mean())
    rgb, alpha, report = punch_regions(
        rgb, alpha, seeds, tol=tol, edge=edge, mode=mode, radius=radius
    )

    shrink_stats = None
    if shrink > 0:
        alpha, shrink_stats = br.shrink_alpha(alpha, shrink)

    dst = Path(out) if out else src
    try:
        br.save_rgba(rgb, alpha, dst, compress)
    except Exception as exc:
        return {"ok": False, "error": "save_failed", "message": str(exc)}

    after = float((alpha >= 0.5).mean())
    done = [r for r in report if r.get("ok")]
    return {
        "ok": bool(done),
        "src": str(src),
        "path": str(dst),
        "seeds": len(seeds),
        "punched": len(done),
        "missed": len(report) - len(done),
        "punched_px": sum(r.get("punched_px", 0) for r in report),
        "opaque_before": round(before, 4),
        "opaque_after": round(after, 4),
        "shrink": shrink_stats,
        "points": report,
        "notes": notes,
        "elapsed_sec": round(time.time() - started, 2),
    }


# ---------------------------------------------------------------------------
# Step 3: cut the fringe
# ---------------------------------------------------------------------------


def shrink_image(
    image: str | Path,
    px: int = 2,
    out: str | Path | None = None,
    soft: float = 0.5,
    min_island: int = 6,
    compress: int = 6,
) -> dict:
    """Cut px pixels inward along every boundary with transparency.

    Without `out` it overwrites the source — this is a finishing step on an already cut file.
    """
    started = time.time()
    src = Path(image)
    if not src.is_file():
        return {"ok": False, "error": "not_found", "message": f"No file: {src}"}

    rgb, alpha = load_rgba(src)
    if float((alpha < 0.5).mean()) < 0.001:
        return {
            "ok": False,
            "error": "no_alpha",
            "message": "No transparent pixels — there is nothing to cut from",
            "src": str(src),
        }

    alpha, stats = br.shrink_alpha(alpha, px, soft=soft, min_island=min_island)
    dst = Path(out) if out else src
    try:
        br.save_rgba(rgb, alpha, dst, compress)
    except Exception as exc:
        return {"ok": False, "error": "save_failed", "message": str(exc)}

    return {
        "ok": True,
        "src": str(src),
        "path": str(dst),
        **stats,
        "elapsed_sec": round(time.time() - started, 2),
    }


def shrink_batch(images, px: int = 2, out_dir=None, recursive: bool = False, **kwargs) -> dict:
    """Cut the fringe off a batch of files (the parts of one device)."""
    files = br.expand_inputs(images, recursive)
    started = time.time()
    results = []
    for i, f in enumerate(files, 1):
        dst = (Path(out_dir) / f.name) if out_dir else None
        res = shrink_image(f, px=px, out=dst, **kwargs)
        log_line = (
            f"[{i}/{len(files)}] {f.name} -> cut {res.get('removed_px', 0)} px"
            if res["ok"]
            else f"[{i}/{len(files)}] {f.name} ERROR {res.get('error')}"
        )
        br.log(log_line)
        results.append(res)

    done = [r for r in results if r["ok"]]
    return {
        "ok": bool(done) and len(done) == len(results),
        "total": len(results),
        "done": len(done),
        "failed": len(results) - len(done),
        "px": px,
        "elapsed_sec": round(time.time() - started, 2),
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="Finishing a cut-out PNG: holes and the fringe")
    sub = p.add_subparsers(dest="cmd", required=True)

    ins = sub.add_parser("inspect", help="Find the unpunched white blobs and draw a map")
    ins.add_argument("image")
    ins.add_argument("--out-dir")
    ins.add_argument("--tol", type=float, default=30.0)
    ins.add_argument("--min-area", type=int, default=40)
    ins.add_argument("--max-regions", type=int, default=60)

    pun = sub.add_parser("punch", help="Knock out white by points/ids from the map")
    pun.add_argument("image")
    pun.add_argument("--ids", help="Blob numbers from the map: 1,4,7")
    pun.add_argument("--points", help="Points in pixels of the original: 512x300,900x412")
    pun.add_argument("--map", dest="map_path")
    pun.add_argument("--out")
    pun.add_argument("--tol", type=float, default=30.0)
    pun.add_argument("--edge", type=int, default=2)
    pun.add_argument("--mode", choices=["flood", "ball"], default="flood")
    pun.add_argument("--radius", type=int, default=24)
    pun.add_argument("--shrink", type=int, default=0)

    shr = sub.add_parser("shrink", help="Cut N px inward along the whole boundary")
    shr.add_argument("images", nargs="+")
    shr.add_argument("--px", type=int, default=2)
    shr.add_argument("--out-dir")
    shr.add_argument("--soft", type=float, default=0.5)
    shr.add_argument("--min-island", type=int, default=6)
    shr.add_argument("--recursive", action="store_true")

    args = p.parse_args()

    if args.cmd == "inspect":
        res = inspect_image(
            args.image,
            out_dir=args.out_dir,
            tol=args.tol,
            min_area=args.min_area,
            max_regions=args.max_regions,
        )
    elif args.cmd == "punch":
        ids = [int(v) for v in args.ids.replace(";", ",").split(",") if v.strip()] if args.ids else None
        points = [v.strip() for v in args.points.replace(";", ",").split(",")] if args.points else None
        res = punch_image(
            args.image,
            points=points,
            ids=ids,
            map_path=args.map_path,
            out=args.out,
            tol=args.tol,
            edge=args.edge,
            mode=args.mode,
            radius=args.radius,
            shrink=args.shrink,
        )
    else:
        res = shrink_batch(
            args.images,
            px=args.px,
            out_dir=args.out_dir,
            recursive=args.recursive,
            soft=args.soft,
            min_island=args.min_island,
        )

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
