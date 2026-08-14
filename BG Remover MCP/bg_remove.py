#!/usr/bin/env python3
"""
Removing the background from a picture locally: the output is a PNG with transparency.

Two engines:
  white — a white background is cut algorithmically (numpy+scipy, nothing downloaded).
          A fill from the frame edges looks for connected white background, so white
          parts INSIDE the object stay opaque. Semi-transparency along the edge is
          taken from the "whiteness" of the pixel, then the colour is unpremultiplied —
          no white fringe is left along the contour.
  ai    — a local neural net (rembg / U^2-Net, ONNX on CPU). Handles any
          background, not only white. The model is downloaded once into ~/.u2net.

  auto  — looks at the frame edges to see whether the background is white: white -> white, otherwise -> ai.

Usage:
  python bg_remove.py in.jpg
  python bg_remove.py in.jpg --out C:\\cut --method ai --trim
  python bg_remove.py "C:\\images\\*.jpeg" --method white --tol-bg 14
  python bg_remove.py --check           # what is available on the system

Installation:
  pip install pillow numpy scipy          # the white engine
  pip install rembg onnxruntime           # the ai engine (plus a ~180 MB model)
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None  # 4K+ upscales must not trip the decompression-bomb guard

SUPPORTED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

# rembg models: general — universal, human — people, anime — drawings.
AI_MODELS = {
    "general": "isnet-general-use",
    "u2net": "u2net",
    "human": "u2net_human_seg",
    "anime": "isnet-anime",
    "fast": "u2netp",
}


def _init_stream(stream):
    """UTF-8 on output: the Windows console otherwise chokes on non-ASCII."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return stream


_init_stream(sys.stderr)
_init_stream(sys.stdout)


def log(msg: str) -> None:
    """Log to stderr: stdout is taken by the MCP protocol."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Engine 1: cutting out a white background (numpy + scipy, no neural net)
# ---------------------------------------------------------------------------


def load_rgb(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Read a picture as RGB float32 0..255 and its alpha, if there was one."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        arr = np.asarray(rgba, dtype=np.float32)
        return arr[..., :3], arr[..., 3] / 255.0
    return np.asarray(img.convert("RGB"), dtype=np.float32), None


def whiteness_distance(rgb: np.ndarray) -> np.ndarray:
    """How far a pixel is from pure white, 0..255.

    We take the maximum over channels of (255 - c): such a criterion catches grey
    and coloured pixels alike and stands up to a slightly yellowish background.
    """
    return (255.0 - rgb).max(axis=2)


def border_is_white(rgb: np.ndarray, tol: float, share: float = 0.9) -> bool:
    """Is the background white judging by the frame border (for method=auto)."""
    d = whiteness_distance(rgb)
    band = max(2, min(d.shape[0], d.shape[1]) // 100)
    edges = np.concatenate(
        [d[:band].ravel(), d[-band:].ravel(), d[:, :band].ravel(), d[:, -band:].ravel()]
    )
    return float((edges <= tol).mean()) >= share


def remove_white_bg(
    rgb: np.ndarray,
    tol_bg: float = 12.0,
    tol_fg: float = 45.0,
    edge: int = 2,
    feather: float = 0.0,
    keep_holes: bool = True,
) -> np.ndarray:
    """An alpha channel 0..1 for a picture on a white background.

    tol_bg — what we count as pure background (the distance to white);
    tol_fg — from what distance a pixel is fully opaque;
    edge   — the width of the semi-transparent band along the contour, px;
    keep_holes — do not knock out white areas unconnected to the frame border
                 (highlights, white parts of the object, "holes" inside the contour).
    """
    dist = whiteness_distance(rgb)
    near_white = dist <= tol_bg

    if keep_holes:
        # Background = only those white areas that reach the frame border.
        labels, n = ndimage.label(near_white)
        if n:
            border_ids = np.unique(
                np.concatenate(
                    [labels[0], labels[-1], labels[:, 0], labels[:, -1]]
                )
            )
            # A "component number -> is background" table: cheaper than np.isin
            # over several thousand labels on a 4-megapixel frame.
            keep = np.zeros(n + 1, dtype=bool)
            keep[border_ids[border_ids != 0]] = True
            background = keep[labels]
        else:
            background = np.zeros_like(near_white)
    else:
        background = near_white

    # The band along the contour: alpha is gradual there, not 0/1 — otherwise it stairsteps.
    if edge > 0:
        band = ndimage.binary_dilation(background, iterations=int(edge)) & ~background
    else:
        band = np.zeros_like(background)

    soft = np.clip((dist - tol_bg) / max(tol_fg - tol_bg, 1e-6), 0.0, 1.0)

    alpha = np.ones(dist.shape, dtype=np.float32)
    alpha[band] = soft[band]
    alpha[background] = 0.0

    if feather > 0:
        alpha = ndimage.gaussian_filter(alpha, sigma=float(feather))

    return alpha


def disk(radius: int) -> np.ndarray:
    """A round structuring element: diagonals get eaten the same as straights."""
    r = int(max(radius, 1))
    y, x = np.ogrid[-r : r + 1, -r : r + 1]
    return (x * x + y * y) <= r * r + 0.5


def shrink_alpha(
    alpha: np.ndarray,
    px: int = 2,
    thr: float = 0.5,
    soft: float = 0.5,
    min_island: int = 6,
) -> tuple[np.ndarray, dict]:
    """Cut `px` pixels inward along every boundary with transparency.

    A cutout always leaves a thin light fringe of background along the contour:
    a pixel on the boundary is half background, and no defringe will save it.
    It is cheaper not to guess the colour but simply to throw those pixels away —
    the erosion runs along the outer contour and around the holes alike, that is
    everywhere opaque touches transparent.

    Outside the frame counts as "opaque" (border_value=1): a part butting against
    the frame border must not be trimmed there.

    min_island — throw away crumbs smaller than N pixels left after the erosion.
    Returns (the new alpha, statistics).
    """
    px = int(px)
    if px <= 0:
        return alpha, {"shrink_px": 0}

    solid = alpha >= thr
    before = int(solid.sum())
    eroded = ndimage.binary_erosion(solid, structure=disk(px), border_value=1)

    if min_island > 0 and eroded.any():
        labels, n = ndimage.label(eroded)
        if n:
            sizes = np.bincount(labels.ravel())
            small = np.zeros(n + 1, dtype=bool)
            small[1:] = sizes[1:] < int(min_island)
            eroded &= ~small[labels]

    mask = eroded.astype(np.float32)
    if soft > 0:
        # A light blur of the mask brings back the single semi-transparent pixel
        # on the edge — without it the edge stairsteps after the erosion.
        mask = ndimage.gaussian_filter(mask, sigma=float(soft))

    out = np.minimum(alpha, mask)
    after = int((out >= thr).sum())
    stats = {
        "shrink_px": px,
        "solid_before": before,
        "solid_after": after,
        "removed_px": before - after,
        "removed_share": round((before - after) / before, 4) if before else 0.0,
    }
    if before and (before - after) / before > 0.35:
        stats["warning"] = "more than a third of the object was cut — px is too large for this part"
    return out, stats


def defringe(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Remove the white fringe: unpremultiply the colour picked up off the white background.

    A semi-transparent pixel = the object's colour over white. We return the clean
    object colour, otherwise a light halo stays along the contour.
    """
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    mask = (a > 0.02) & (a < 0.999)
    pure = np.where(mask, (rgb - 255.0 * (1.0 - a)) / np.maximum(a, 1e-3), rgb)
    return np.clip(pure, 0, 255)


# ---------------------------------------------------------------------------
# Engine 2: a local neural net (rembg / U^2-Net)
# ---------------------------------------------------------------------------

_AI_SESSIONS: dict[str, object] = {}


def ai_available() -> tuple[bool, str]:
    try:
        import rembg  # noqa: F401
    except ImportError:
        return False, "rembg is not installed (pip install rembg onnxruntime)"
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "onnxruntime is not installed (pip install onnxruntime)"
    return True, "ok"


def ai_session(model: str):
    """A rembg session. The model is downloaded once into ~/.u2net and cached."""
    name = AI_MODELS.get(model, model)
    if name not in _AI_SESSIONS:
        from rembg import new_session

        log(f"Loading model {name} (first time — a ~180 MB download)")
        _AI_SESSIONS[name] = new_session(name)
    return _AI_SESSIONS[name]


def remove_bg_ai(
    path: Path,
    model: str = "general",
    alpha_matting: bool = False,
    post_process: bool = True,
) -> np.ndarray:
    """Alpha 0..1 from the neural net."""
    from rembg import remove

    src = Image.open(path).convert("RGB")
    out = remove(
        src,
        session=ai_session(model),
        alpha_matting=alpha_matting,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=15,
        alpha_matting_erode_size=8,
        post_process_mask=post_process,
    )
    return np.asarray(out.convert("RGBA"), dtype=np.float32)[..., 3] / 255.0


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def trim_to_content(rgb: np.ndarray, alpha: np.ndarray, pad: int = 0, thr: float = 0.02):
    """Crop the transparent margins at the edges."""
    ys, xs = np.where(alpha > thr)
    if not len(ys):
        return rgb, alpha, None
    y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + 1 + pad, alpha.shape[0])
    x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + 1 + pad, alpha.shape[1])
    return rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1], (x0, y0, x1, y1)


def save_rgba(rgb: np.ndarray, alpha: np.ndarray, path: Path, compress: int = 6) -> None:
    """PNG RGBA. compress=9/optimize gains only ~5% of the size but takes 8x longer."""
    rgba = np.dstack(
        [np.clip(rgb, 0, 255), np.clip(alpha * 255.0, 0, 255)]
    ).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(
        path, "PNG", compress_level=int(np.clip(compress, 0, 9))
    )


def out_path_for(src: Path, out_dir: str | Path | None, suffix: str) -> Path:
    root = Path(out_dir) if out_dir else src.parent
    return root / f"{src.stem}{suffix}.png"


def remove_background(
    image: str | Path,
    out: str | Path | None = None,
    method: str = "auto",
    tol_bg: float = 12.0,
    tol_fg: float = 45.0,
    edge: int = 2,
    feather: float = 0.0,
    trim: bool = False,
    pad: int = 0,
    keep_holes: bool = True,
    shrink: int = 0,
    shrink_soft: float = 0.5,
    min_island: int = 6,
    ai_model: str = "general",
    alpha_matting: bool = False,
    suffix: str = "_nobg",
    overwrite: bool = True,
    compress: int = 6,
) -> dict:
    """Remove the background from one picture. Returns a dict with the result.

    `out` — a folder OR a concrete .png path. If unset, the file lands next to the
    source as <name>_nobg.png.
    """
    started = time.time()
    src = Path(image)
    if not src.is_file():
        return {"ok": False, "error": "not_found", "message": f"No file: {src}", "src": str(src)}

    try:
        rgb, src_alpha = load_rgb(src)
    except Exception as exc:
        return {"ok": False, "error": "unreadable", "message": str(exc), "src": str(src)}

    h, w = rgb.shape[:2]
    chosen = method
    notes: list[str] = []

    if method == "auto":
        if border_is_white(rgb, tol_bg * 2):
            chosen = "white"
        else:
            ok, why = ai_available()
            chosen = "ai" if ok else "white"
            if not ok:
                notes.append(f"the background is not white, but the AI is unavailable ({why}) — cutting as white")

    if chosen == "ai":
        ok, why = ai_available()
        if not ok:
            return {"ok": False, "error": "ai_unavailable", "message": why, "src": str(src)}
        try:
            alpha = remove_bg_ai(src, ai_model, alpha_matting)
        except Exception as exc:
            return {
                "ok": False,
                "error": "ai_failed",
                "message": str(exc).splitlines()[0],
                "src": str(src),
            }
        if feather > 0:
            alpha = ndimage.gaussian_filter(alpha, sigma=float(feather))
        # the AI sometimes leaves a light fringe of white background — cleaned with the same call.
        rgb = defringe(rgb, alpha)
    elif chosen == "white":
        alpha = remove_white_bg(rgb, tol_bg, tol_fg, edge, feather, keep_holes)
        rgb = defringe(rgb, alpha)
    else:
        return {
            "ok": False,
            "error": "bad_method",
            "message": f"method must be auto/white/ai, not {method!r}",
            "src": str(src),
        }

    if src_alpha is not None:
        alpha = alpha * src_alpha  # respect the source's own transparency
        notes.append("the source already had alpha — multiplied it in")

    shrink_stats = None
    if shrink > 0:
        alpha, shrink_stats = shrink_alpha(alpha, shrink, soft=shrink_soft, min_island=min_island)
        if "warning" in shrink_stats:
            notes.append(shrink_stats["warning"])

    coverage = float((alpha > 0.5).mean())
    if coverage >= 0.999:
        notes.append("background not found: almost everything stayed opaque")
    elif coverage <= 0.001:
        notes.append("almost everything was cut away — check tol_bg/method")

    box = None
    if trim:
        rgb, alpha, box = trim_to_content(rgb, alpha, pad)

    dst = Path(out) if out and str(out).lower().endswith(".png") else out_path_for(src, out, suffix)
    if dst.exists() and not overwrite:
        return {"ok": False, "error": "exists", "message": f"The file already exists: {dst}", "src": str(src)}

    try:
        save_rgba(rgb, alpha, dst, compress)
    except Exception as exc:
        return {"ok": False, "error": "save_failed", "message": str(exc), "src": str(src)}

    return {
        "ok": True,
        "src": str(src),
        "path": str(dst),
        "filename": dst.name,
        "method": chosen,
        "ai_model": ai_model if chosen == "ai" else None,
        "width": int(alpha.shape[1]),
        "height": int(alpha.shape[0]),
        "source_size": [w, h],
        "bytes": dst.stat().st_size,
        "opaque_share": round(coverage, 4),
        "shrink": shrink_stats,
        "trimmed_box": box,
        "elapsed_sec": round(time.time() - started, 2),
        "notes": notes,
    }


def expand_inputs(items: Iterable[str | Path], recursive: bool = False) -> list[Path]:
    """Expand paths/masks/folders into a list of image files."""
    found: list[Path] = []
    for item in items:
        text = str(item).strip().strip('"')
        p = Path(text)
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            found += [f for f in sorted(p.glob(pattern)) if f.suffix.lower() in SUPPORTED_EXT]
        elif any(ch in text for ch in "*?["):
            found += [
                Path(f)
                for f in sorted(globmod.glob(text, recursive=recursive))
                if Path(f).suffix.lower() in SUPPORTED_EXT
            ]
        else:
            found.append(p)
    # duplicates are removed, the order is kept
    return list(dict.fromkeys(found))


def remove_background_batch(
    images: Iterable[str | Path],
    out_dir: str | Path | None = None,
    recursive: bool = False,
    **kwargs,
) -> dict:
    """In a batch. Skips ready _nobg.png files so we do not chew our own output."""
    suffix = kwargs.get("suffix", "_nobg")
    files = [f for f in expand_inputs(images, recursive) if not f.stem.endswith(suffix)]
    started = time.time()
    results = []
    for i, f in enumerate(files, 1):
        log(f"[{i}/{len(files)}] {f.name}")
        res = remove_background(f, out=out_dir, **kwargs)
        if res["ok"]:
            log(f"    -> {res['filename']} ({res['method']}, "
                f"object {res['opaque_share'] * 100:.1f}% of the frame, {res['elapsed_sec']} s)")
        else:
            log(f"    ERROR {res['error']}: {res['message']}")
        results.append(res)

    done = [r for r in results if r["ok"]]
    return {
        "ok": bool(done) and len(done) == len(results),
        "total": len(results),
        "done": len(done),
        "failed": len(results) - len(done),
        "out_dir": str(out_dir) if out_dir else None,
        "elapsed_sec": round(time.time() - started, 2),
        "results": results,
    }


def check_environment() -> dict:
    """What is available: libraries, the AI engine, the model cache."""
    import importlib.metadata as md

    def ver(name: str) -> str | None:
        try:
            return md.version(name)
        except Exception:
            return None

    ok_ai, why = ai_available()
    cache = Path.home() / ".u2net"
    models = sorted(f.name for f in cache.glob("*.onnx")) if cache.exists() else []

    return {
        "ready": True,  # the white engine always works: pillow+numpy+scipy are mandatory
        "pillow": ver("pillow"),
        "numpy": ver("numpy"),
        "scipy": ver("scipy"),
        "rembg": ver("rembg"),
        "onnxruntime": ver("onnxruntime") or ver("onnxruntime-gpu"),
        "ai_ready": ok_ai,
        "ai_reason": why,
        "ai_models_cached": models,
        "ai_models_cache_dir": str(cache),
        "methods": ["white", "ai"] if ok_ai else ["white"],
        "message": (
            "Both engines ready"
            if ok_ai
            else f"Only white works (white background). AI: {why}"
        ),
    }


def install_ai(gpu: bool = False) -> dict:
    """Install rembg+onnxruntime into the current Python. Downloads ~100 MB of wheels."""
    import subprocess

    pkgs = ["rembg", "onnxruntime-gpu" if gpu else "onnxruntime"]
    log(f"pip install {' '.join(pkgs)}")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pkgs],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    ok, why = ai_available()
    return {
        "ok": proc.returncode == 0 and ok,
        "returncode": proc.returncode,
        "ai_ready": ok,
        "ai_reason": why,
        "tail": (proc.stdout or "")[-1200:] + (proc.stderr or "")[-800:],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Local background removal: the output is a PNG with transparency"
    )
    p.add_argument("images", nargs="*", help="Files, folders or masks (*.jpeg)")
    p.add_argument("--out", help="Output folder (next to the source by default)")
    p.add_argument(
        "--method",
        choices=["auto", "white", "ai"],
        default="auto",
        help="auto (by the frame edges), white (white background, offline), ai (neural net)",
    )
    p.add_argument(
        "--ai-model",
        choices=list(AI_MODELS),
        default="general",
        help="Model for --method ai",
    )
    p.add_argument("--alpha-matting", action="store_true", help="Soft edge from the AI (slower)")
    p.add_argument("--tol-bg", type=float, default=12.0, help="The 'this is background' threshold, 0..255 (12)")
    p.add_argument("--tol-fg", type=float, default=45.0, help="Full-opacity threshold (45)")
    p.add_argument("--edge", type=int, default=2, help="Width of the soft edge, px (2)")
    p.add_argument("--feather", type=float, default=0.0, help="Alpha blur, sigma (0)")
    p.add_argument("--trim", action="store_true", help="Crop the transparent margins")
    p.add_argument("--pad", type=int, default=0, help="Padding when using --trim, px")
    p.add_argument(
        "--no-keep-holes",
        action="store_true",
        help="Knock out ALL white pixels, including inner ones (they stay by default)",
    )
    p.add_argument(
        "--shrink",
        type=int,
        default=0,
        help="Cut N px inward along every boundary with transparency — removes the white fringe (0)",
    )
    p.add_argument("--shrink-soft", type=float, default=0.5, help="Edge smoothing after the cut")
    p.add_argument("--min-island", type=int, default=6, help="Throw away pieces smaller than N px (6)")
    p.add_argument("--suffix", default="_nobg", help="Suffix of the output file name")
    p.add_argument(
        "--compress", type=int, default=6, help="PNG compression 0-9 (6). 9 is 8x slower"
    )
    p.add_argument("--recursive", action="store_true", help="Walk folders recursively")
    p.add_argument("--no-overwrite", action="store_true", help="Do not overwrite finished files")
    p.add_argument("--check", action="store_true", help="Show the environment and exit")
    p.add_argument("--install-ai", action="store_true", help="Install rembg+onnxruntime")
    p.add_argument("--json", action="store_true", help="Print the result as JSON to stdout")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.check:
        print(json.dumps(check_environment(), ensure_ascii=False, indent=2))
        return 0

    if args.install_ai:
        res = install_ai()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1

    if not args.images:
        log("Nothing to cut: pass files, a folder or a mask. Help: --help")
        return 2

    res = remove_background_batch(
        args.images,
        out_dir=args.out,
        recursive=args.recursive,
        method=args.method,
        tol_bg=args.tol_bg,
        tol_fg=args.tol_fg,
        edge=args.edge,
        feather=args.feather,
        trim=args.trim,
        pad=args.pad,
        keep_holes=not args.no_keep_holes,
        shrink=args.shrink,
        shrink_soft=args.shrink_soft,
        min_island=args.min_island,
        ai_model=args.ai_model,
        alpha_matting=args.alpha_matting,
        suffix=args.suffix,
        overwrite=not args.no_overwrite,
        compress=args.compress,
    )

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    log(f"TOTAL: {res['done']} of {res['total']} in {res['elapsed_sec']} s")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
