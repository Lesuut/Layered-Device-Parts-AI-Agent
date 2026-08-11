#!/usr/bin/env python3
"""
Удаление фона с картинки локально: на выходе PNG с прозрачностью.

Два движка:
  white — белый фон вырезается алгоритмически (numpy+scipy, ничего не качает).
          Заливка от краёв кадра ищет связный белый фон, поэтому белые детали
          ВНУТРИ объекта остаются непрозрачными. Полупрозрачность на кромке
          берётся из «белизны» пикселя, затем цвет распремножается —
          белая кайма по контуру не остаётся.
  ai    — локальная нейросеть (rembg / U^2-Net, ONNX на CPU). Работает с любым
          фоном, не только белым. Модель качается один раз в ~/.u2net.

  auto  — по краям кадра смотрит, белый ли фон: белый -> white, иначе -> ai.

Запуск:
  python bg_remove.py in.jpg
  python bg_remove.py in.jpg --out C:\\cut --method ai --trim
  python bg_remove.py "C:\\images\\*.jpeg" --method white --tol-bg 14
  python bg_remove.py --check           # что доступно в системе

Установка:
  pip install pillow numpy scipy          # движок white
  pip install rembg onnxruntime           # движок ai (плюс ~180 МБ модель)
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

Image.MAX_IMAGE_PIXELS = None  # апскейлы 4K+ не должны падать на защите от бомб

SUPPORTED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

# Модели rembg: general — универсальная, human — люди, anime — рисовка.
AI_MODELS = {
    "general": "isnet-general-use",
    "u2net": "u2net",
    "human": "u2net_human_seg",
    "anime": "isnet-anime",
    "fast": "u2netp",
}


def _init_stream(stream):
    """UTF-8 на выводе: консоль Windows иначе падает на кириллице."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return stream


_init_stream(sys.stderr)
_init_stream(sys.stdout)


def log(msg: str) -> None:
    """Лог в stderr: stdout занят протоколом MCP."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Движок 1: вырезание белого фона (numpy + scipy, без нейросети)
# ---------------------------------------------------------------------------


def load_rgb(path: Path) -> tuple[np.ndarray, np.ndarray | None]:
    """Прочитать картинку как RGB float32 0..255 и её альфу, если была."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        arr = np.asarray(rgba, dtype=np.float32)
        return arr[..., :3], arr[..., 3] / 255.0
    return np.asarray(img.convert("RGB"), dtype=np.float32), None


def whiteness_distance(rgb: np.ndarray) -> np.ndarray:
    """Насколько пиксель далёк от чистого белого, 0..255.

    Берём максимум по каналам от (255 - c): такой критерий одинаково ловит
    и серый, и цветной пиксель, а к лёгкой желтизне фона устойчив.
    """
    return (255.0 - rgb).max(axis=2)


def border_is_white(rgb: np.ndarray, tol: float, share: float = 0.9) -> bool:
    """Белый ли фон по рамке кадра (для method=auto)."""
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
    """Альфа-канал 0..1 для картинки на белом фоне.

    tol_bg — что считаем чистым фоном (расстояние до белого);
    tol_fg — с какого расстояния пиксель полностью непрозрачен;
    edge   — ширина полосы полупрозрачности вдоль контура, px;
    keep_holes — не выбивать белые области, не связанные с краем кадра
                 (блики, белые детали объекта, «дырки» внутри контура).
    """
    dist = whiteness_distance(rgb)
    near_white = dist <= tol_bg

    if keep_holes:
        # Фон = только те белые области, что дотягиваются до рамки кадра.
        labels, n = ndimage.label(near_white)
        if n:
            border_ids = np.unique(
                np.concatenate(
                    [labels[0], labels[-1], labels[:, 0], labels[:, -1]]
                )
            )
            # Таблица «номер компоненты -> это фон»: дешевле, чем np.isin
            # по нескольким тысячам меток на 4-мегапиксельном кадре.
            keep = np.zeros(n + 1, dtype=bool)
            keep[border_ids[border_ids != 0]] = True
            background = keep[labels]
        else:
            background = np.zeros_like(near_white)
    else:
        background = near_white

    # Полоса вдоль контура: там альфа плавная, а не 0/1 — иначе «лесенка».
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
    """Круглый структурный элемент: диагонали съедаются так же, как прямые."""
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
    """Срезать `px` пикселей вглубь по всей границе с прозрачностью.

    Вырезание всегда оставляет по контуру тонкую светлую кайму от фона:
    пиксель на границе наполовину фоновый, и никакой defringe его не спасёт.
    Дешевле не угадывать цвет, а просто выбросить эти пиксели — эрозия идёт
    и по внешнему контуру, и вокруг дыр, то есть везде, где непрозрачное
    касается прозрачного.

    За кадром считаем «непрозрачно» (border_value=1): деталь, упирающаяся
    в рамку кадра, не должна там обрезаться.

    min_island — выкинуть оставшиеся после эрозии крошки меньше N пикселей.
    Возвращает (новая альфа, статистика).
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
        # Лёгкое размытие маски возвращает единственный полупрозрачный пиксель
        # на кромке — без него после эрозии край становится «лесенкой».
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
        stats["warning"] = "срезано больше трети объекта — px слишком большой для этой детали"
    return out, stats


def defringe(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Убрать белую кайму: распремножить цвет, снятый с белого фона.

    Полупрозрачный пиксель = цвет объекта поверх белого. Возвращаем чистый
    цвет объекта, иначе по контуру останется светлый ореол.
    """
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    mask = (a > 0.02) & (a < 0.999)
    pure = np.where(mask, (rgb - 255.0 * (1.0 - a)) / np.maximum(a, 1e-3), rgb)
    return np.clip(pure, 0, 255)


# ---------------------------------------------------------------------------
# Движок 2: локальная нейросеть (rembg / U^2-Net)
# ---------------------------------------------------------------------------

_AI_SESSIONS: dict[str, object] = {}


def ai_available() -> tuple[bool, str]:
    try:
        import rembg  # noqa: F401
    except ImportError:
        return False, "не установлен rembg (pip install rembg onnxruntime)"
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False, "не установлен onnxruntime (pip install onnxruntime)"
    return True, "ok"


def ai_session(model: str):
    """Сессия rembg. Модель качается один раз в ~/.u2net и кешируется."""
    name = AI_MODELS.get(model, model)
    if name not in _AI_SESSIONS:
        from rembg import new_session

        log(f"Гружу модель {name} (первый раз — скачивание ~180 МБ)")
        _AI_SESSIONS[name] = new_session(name)
    return _AI_SESSIONS[name]


def remove_bg_ai(
    path: Path,
    model: str = "general",
    alpha_matting: bool = False,
    post_process: bool = True,
) -> np.ndarray:
    """Альфа 0..1 от нейросети."""
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
# Общая обвязка
# ---------------------------------------------------------------------------


def trim_to_content(rgb: np.ndarray, alpha: np.ndarray, pad: int = 0, thr: float = 0.02):
    """Обрезать прозрачные поля по краям."""
    ys, xs = np.where(alpha > thr)
    if not len(ys):
        return rgb, alpha, None
    y0, y1 = max(int(ys.min()) - pad, 0), min(int(ys.max()) + 1 + pad, alpha.shape[0])
    x0, x1 = max(int(xs.min()) - pad, 0), min(int(xs.max()) + 1 + pad, alpha.shape[1])
    return rgb[y0:y1, x0:x1], alpha[y0:y1, x0:x1], (x0, y0, x1, y1)


def save_rgba(rgb: np.ndarray, alpha: np.ndarray, path: Path, compress: int = 6) -> None:
    """PNG RGBA. compress=9/optimize даёт всего ~5% размера, но в 8 раз дольше."""
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
    """Убрать фон у одной картинки. Возвращает словарь с результатом.

    `out` — папка ИЛИ конкретный путь .png. Если не задан, файл ляжет рядом
    с исходником как <имя>_nobg.png.
    """
    started = time.time()
    src = Path(image)
    if not src.is_file():
        return {"ok": False, "error": "not_found", "message": f"Нет файла: {src}", "src": str(src)}

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
                notes.append(f"фон не белый, но ИИ недоступен ({why}) — режу как белый")

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
        # ИИ иногда оставляет светлую кайму от белого фона — подчищаем ею же.
        rgb = defringe(rgb, alpha)
    elif chosen == "white":
        alpha = remove_white_bg(rgb, tol_bg, tol_fg, edge, feather, keep_holes)
        rgb = defringe(rgb, alpha)
    else:
        return {
            "ok": False,
            "error": "bad_method",
            "message": f"method должен быть auto/white/ai, а не {method!r}",
            "src": str(src),
        }

    if src_alpha is not None:
        alpha = alpha * src_alpha  # уважаем прозрачность исходника
        notes.append("у исходника уже была альфа — перемножил")

    shrink_stats = None
    if shrink > 0:
        alpha, shrink_stats = shrink_alpha(alpha, shrink, soft=shrink_soft, min_island=min_island)
        if "warning" in shrink_stats:
            notes.append(shrink_stats["warning"])

    coverage = float((alpha > 0.5).mean())
    if coverage >= 0.999:
        notes.append("фон не найден: почти всё осталось непрозрачным")
    elif coverage <= 0.001:
        notes.append("вырезалось почти всё — проверь tol_bg/метод")

    box = None
    if trim:
        rgb, alpha, box = trim_to_content(rgb, alpha, pad)

    dst = Path(out) if out and str(out).lower().endswith(".png") else out_path_for(src, out, suffix)
    if dst.exists() and not overwrite:
        return {"ok": False, "error": "exists", "message": f"Файл уже есть: {dst}", "src": str(src)}

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
    """Разложить пути/маски/папки в список файлов картинок."""
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
    # дубликаты убираем, порядок сохраняем
    return list(dict.fromkeys(found))


def remove_background_batch(
    images: Iterable[str | Path],
    out_dir: str | Path | None = None,
    recursive: bool = False,
    **kwargs,
) -> dict:
    """Пачкой. Пропускает уже готовые _nobg.png, чтобы не жевать свой вывод."""
    suffix = kwargs.get("suffix", "_nobg")
    files = [f for f in expand_inputs(images, recursive) if not f.stem.endswith(suffix)]
    started = time.time()
    results = []
    for i, f in enumerate(files, 1):
        log(f"[{i}/{len(files)}] {f.name}")
        res = remove_background(f, out=out_dir, **kwargs)
        if res["ok"]:
            log(f"    -> {res['filename']} ({res['method']}, "
                f"объект {res['opaque_share'] * 100:.1f}% кадра, {res['elapsed_sec']} с)")
        else:
            log(f"    ОШИБКА {res['error']}: {res['message']}")
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
    """Что доступно: библиотеки, движок ИИ, кеш моделей."""
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
        "ready": True,  # движок white работает всегда: pillow+numpy+scipy обязательны
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
            "Оба движка готовы"
            if ok_ai
            else f"Работает только white (белый фон). ИИ: {why}"
        ),
    }


def install_ai(gpu: bool = False) -> dict:
    """Поставить rembg+onnxruntime в текущий Python. Качает ~100 МБ колёс."""
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
        description="Локальное удаление фона: на выходе PNG с прозрачностью"
    )
    p.add_argument("images", nargs="*", help="Файлы, папки или маски (*.jpeg)")
    p.add_argument("--out", help="Папка вывода (по умолчанию рядом с исходником)")
    p.add_argument(
        "--method",
        choices=["auto", "white", "ai"],
        default="auto",
        help="auto (по краям кадра), white (белый фон, без сети), ai (нейросеть)",
    )
    p.add_argument(
        "--ai-model",
        choices=list(AI_MODELS),
        default="general",
        help="Модель для --method ai",
    )
    p.add_argument("--alpha-matting", action="store_true", help="Мягкая кромка у ИИ (медленнее)")
    p.add_argument("--tol-bg", type=float, default=12.0, help="Порог «это фон», 0..255 (12)")
    p.add_argument("--tol-fg", type=float, default=45.0, help="Порог полной непрозрачности (45)")
    p.add_argument("--edge", type=int, default=2, help="Ширина мягкой кромки, px (2)")
    p.add_argument("--feather", type=float, default=0.0, help="Размытие альфы, sigma (0)")
    p.add_argument("--trim", action="store_true", help="Обрезать прозрачные поля")
    p.add_argument("--pad", type=int, default=0, help="Отступ при --trim, px")
    p.add_argument(
        "--no-keep-holes",
        action="store_true",
        help="Выбивать ВСЕ белые пиксели, включая внутренние (по умолчанию они остаются)",
    )
    p.add_argument(
        "--shrink",
        type=int,
        default=0,
        help="Срезать N px вглубь по всей границе с прозрачностью — убирает белую кайму (0)",
    )
    p.add_argument("--shrink-soft", type=float, default=0.5, help="Сглаживание кромки после среза")
    p.add_argument("--min-island", type=int, default=6, help="Выкидывать куски мельче N px (6)")
    p.add_argument("--suffix", default="_nobg", help="Суффикс имени выходного файла")
    p.add_argument(
        "--compress", type=int, default=6, help="Сжатие PNG 0-9 (6). 9 медленнее в 8 раз"
    )
    p.add_argument("--recursive", action="store_true", help="Обходить папки рекурсивно")
    p.add_argument("--no-overwrite", action="store_true", help="Не перезаписывать готовые файлы")
    p.add_argument("--check", action="store_true", help="Показать окружение и выйти")
    p.add_argument("--install-ai", action="store_true", help="Поставить rembg+onnxruntime")
    p.add_argument("--json", action="store_true", help="Печатать результат JSON в stdout")
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
        log("Нечего резать: передай файлы, папку или маску. Помощь: --help")
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
    log(f"ИТОГО: {res['done']} из {res['total']} за {res['elapsed_sec']} с")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
