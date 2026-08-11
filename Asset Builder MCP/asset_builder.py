#!/usr/bin/env python3
"""
Сборка 2D-ассета устройства: россыпь деталей -> атлас + JSON сборки.

Конвейер (шаги 4-7 общего пайплайна):
  1. extract_parts  — разрезать PNG с прозрачным фоном на отдельные детали
                      (связные области альфы) и собрать лист-контактку
                      с НОМЕРАМИ, чтобы агент глазами выбрал нужные.
  2. pack_atlas     — выбранные детали упаковать в одну текстуру без наложений
                      и записать device.json с кадрами (frames).
  3. render_assembly— отрисовать сборку по device.json в PNG, чтобы агент
                      глазами проверил, ровно ли всё легло, и подвинул.
  4. package_asset  — сложить готовое: texture.png + device.json + preview.png
                      + viewer.html в одну папку.

Система координат сборки:
  canvas [W,H] — холст устройства в пикселях;
  position [x,y] — ЦЕНТР детали на холсте;
  size [w,h] — размер отрисовки (можно менять, ассет тянется);
  rotation — градусы, по часовой;
  layer — порядок отрисовки, меньше = ниже (0 — самый нижний слой).

Запуск CLI:
  python asset_builder.py extract cut/*.png --out work/parts
  python asset_builder.py pack work/parts/sel.json --out work/atlas --name nokia_3310
  python asset_builder.py render work/atlas/device.json
  python asset_builder.py package work/atlas/device.json --out out/nokia_3310
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None
HERE = Path(__file__).resolve().parent
VIEWER_SRC = HERE / "viewer.html"


def _init_stream(stream):
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


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("arialbd.ttf", "arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def expand_inputs(items: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for item in items:
        text = str(item).strip().strip('"')
        p = Path(text)
        if p.is_dir():
            out += sorted(f for f in p.glob("*.png"))
        elif any(ch in text for ch in "*?["):
            out += [Path(f) for f in sorted(globmod.glob(text))]
        else:
            out.append(p)
    return list(dict.fromkeys(out))


# ---------------------------------------------------------------------------
# Шаг 1: нарезка деталей из картинки с прозрачным фоном
# ---------------------------------------------------------------------------


def extract_parts(
    images: Iterable[str | Path],
    out_dir: str | Path,
    alpha_thr: int = 40,
    min_area_frac: float = 0.0004,
    min_side: int = 14,
    pad: int = 2,
    close_gaps: int = 2,
    max_parts: int = 200,
    sheet_cols: int = 8,
    sheet_cell: int = 220,
) -> dict:
    """Разрезать PNG с альфой на отдельные детали.

    Деталь = связная область непрозрачных пикселей. Мелкий мусор отсекается
    по площади и стороне. Возвращает список деталей и путь к контактке,
    на которой у каждой детали подписан её индекс — по нему агент выбирает.

    close_gaps — морфологическое замыкание в px: склеивает деталь, которую
    антиалиасинг разорвал на куски (тонкие шлейфы, пружины).
    """
    started = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = expand_inputs(images)
    if not files:
        return {"ok": False, "error": "no_input", "message": "Не нашёл входных PNG"}

    parts: list[dict] = []
    idx = 0

    for src in files:
        if not src.is_file():
            log(f"пропускаю (нет файла): {src}")
            continue
        img = Image.open(src).convert("RGBA")
        arr = np.asarray(img)
        alpha = arr[..., 3]
        if (alpha > alpha_thr).mean() > 0.98:
            log(f"ВНИМАНИЕ: {src.name} почти непрозрачный — фон не удалён?")

        mask = alpha > alpha_thr
        if close_gaps > 0:
            mask_lbl = ndimage.binary_closing(mask, iterations=int(close_gaps))
        else:
            mask_lbl = mask

        labels, n = ndimage.label(mask_lbl, structure=np.ones((3, 3), dtype=int))
        if not n:
            log(f"{src.name}: деталей не нашёл")
            continue

        min_area = max(int(min_area_frac * mask.size), 64)
        objects = ndimage.find_objects(labels)
        sizes = ndimage.sum_labels(mask_lbl, labels, index=np.arange(1, n + 1))

        found = 0
        for i, sl in enumerate(objects, start=1):
            if sl is None:
                continue
            area = float(sizes[i - 1])
            ys, xs = sl
            h, w = ys.stop - ys.start, xs.stop - xs.start
            if area < min_area or min(w, h) < min_side:
                continue

            y0 = max(ys.start - pad, 0)
            x0 = max(xs.start - pad, 0)
            y1 = min(ys.stop + pad, arr.shape[0])
            x1 = min(xs.stop + pad, arr.shape[1])

            piece = arr[y0:y1, x0:x1].copy()
            # чужие детали, попавшие в прямоугольник, гасим по метке
            own = labels[y0:y1, x0:x1] == i
            piece[..., 3] = np.where(own, piece[..., 3], 0)

            idx += 1
            name = f"part_{idx:03d}.png"
            Image.fromarray(piece, "RGBA").save(out / name, "PNG", compress_level=6)
            parts.append(
                {
                    "index": idx,
                    "file": str(out / name),
                    "filename": name,
                    "src": str(src),
                    "src_bbox": [int(x0), int(y0), int(x1), int(y1)],
                    "width": int(x1 - x0),
                    "height": int(y1 - y0),
                    "area_px": int(area),
                    "aspect": round((x1 - x0) / max(y1 - y0, 1), 3),
                }
            )
            found += 1
            if idx >= max_parts:
                log(f"Достигнут потолок {max_parts} деталей — останавливаюсь")
                break
        log(f"{src.name}: деталей {found}")
        if idx >= max_parts:
            break

    if not parts:
        return {
            "ok": False,
            "error": "no_parts",
            "message": "Ни одной детали. Фон точно удалён? Понизь min_area_frac/min_side",
        }

    sheet = out / "contact_sheet.png"
    make_contact_sheet(parts, sheet, cols=sheet_cols, cell=sheet_cell)

    manifest = out / "parts.json"
    manifest.write_text(
        json.dumps({"parts": parts, "contact_sheet": str(sheet)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "count": len(parts),
        "out_dir": str(out),
        "contact_sheet": str(sheet),
        "manifest": str(manifest),
        "elapsed_sec": round(time.time() - started, 2),
        "parts": parts,
        "message": (
            f"Нарезано {len(parts)} деталей. Посмотри контактку {sheet} — "
            "на ней у каждой детали подписан индекс, выбирай по индексам."
        ),
    }


def make_contact_sheet(parts: list[dict], path: Path, cols: int = 8, cell: int = 220) -> Path:
    """Лист с пронумерованными деталями — для выбора глазами."""
    rows = (len(parts) + cols - 1) // cols
    label_h = max(22, cell // 8)
    W, H = cols * cell, rows * (cell + label_h)
    sheet = Image.new("RGBA", (W, H), (32, 32, 36, 255))
    draw = ImageDraw.Draw(sheet)
    font = load_font(max(14, cell // 12))

    for i, p in enumerate(parts):
        cx, cy = (i % cols) * cell, (i // cols) * (cell + label_h)
        # шахматка под прозрачностью, чтобы отличать дырки от белого
        for by in range(cy, cy + cell, 16):
            for bx in range(cx, cx + cell, 16):
                if ((bx - cx) // 16 + (by - cy) // 16) % 2 == 0:
                    draw.rectangle([bx, by, bx + 15, by + 15], fill=(60, 60, 66, 255))

        im = Image.open(p["file"]).convert("RGBA")
        im.thumbnail((cell - 12, cell - 12), Image.LANCZOS)
        sheet.alpha_composite(im, (cx + (cell - im.width) // 2, cy + (cell - im.height) // 2))

        draw.rectangle([cx, cy, cx + cell - 1, cy + cell + label_h - 1], outline=(90, 90, 96, 255))
        text = f"#{p['index']}  {p['width']}x{p['height']}"
        draw.text((cx + 6, cy + cell + 3), text, fill=(240, 240, 240, 255), font=font)

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(path, "PNG", compress_level=6)
    return path


def punch_holes(
    files: Iterable[str | Path],
    tol: float = 42.0,
    min_hole_px: int = 24,
    edge: int = 1,
    suffix: str = "",
    out_dir: str | Path | None = None,
) -> dict:
    """Пробить сквозные отверстия в детали: белое внутри -> прозрачное.

    Нужно для корпусов и рамок: вырез под экран, дырки под клавиши, отверстия
    винтов после удаления фона остаются БЕЛЫМИ (их спасает keep_holes), а в
    игровом ассете сквозь них должен просвечивать нижний слой.

    Применять выборочно: у детали вроде белой мембраны или наклейки батареи
    светлые области — это сама деталь, а не отверстия.

    suffix пустой = переписать файл на месте.
    """
    started = time.time()
    done, skipped = [], []
    for f in expand_inputs(files):
        if not f.is_file():
            skipped.append({"file": str(f), "why": "нет файла"})
            continue
        arr = np.asarray(Image.open(f).convert("RGBA")).astype(np.float32)
        rgb, alpha = arr[..., :3], arr[..., 3]
        solid = alpha > 8
        white = ((255.0 - rgb).max(axis=2) <= tol) & solid
        if not white.any():
            skipped.append({"file": str(f), "why": "белых областей внутри нет"})
            continue

        labels, n = ndimage.label(white, structure=np.ones((3, 3), dtype=int))
        sizes = ndimage.sum_labels(white, labels, index=np.arange(1, n + 1))
        keep = np.zeros(n + 1, dtype=bool)
        keep[1:] = sizes >= min_hole_px
        holes = keep[labels]
        if edge > 0:  # чуть расширяем, чтобы не осталась светлая кайма
            holes = ndimage.binary_dilation(holes, iterations=int(edge)) & solid

        new_alpha = np.where(holes, 0.0, alpha)
        punched = float((holes & solid).sum())
        out_arr = np.dstack([rgb, new_alpha]).astype(np.uint8)

        root = Path(out_dir) if out_dir else f.parent
        root.mkdir(parents=True, exist_ok=True)
        dst = root / (f.stem + suffix + ".png")
        Image.fromarray(out_arr, "RGBA").save(dst, "PNG", compress_level=6)
        done.append(
            {
                "file": str(dst),
                "src": str(f),
                "holes": int(keep[1:].sum()),
                "punched_px": int(punched),
                "punched_share": round(punched / max(solid.sum(), 1), 4),
            }
        )

    return {
        "ok": bool(done),
        "punched": done,
        "skipped": skipped,
        "elapsed_sec": round(time.time() - started, 2),
        "message": (
            f"Отверстия пробиты в {len(done)} деталях"
            + (f", пропущено {len(skipped)}" if skipped else "")
            + ". Проверь глазами: не съело ли светлые части самой детали."
        ),
    }


# ---------------------------------------------------------------------------
# Шаг 2: упаковка выбранных деталей в атлас
# ---------------------------------------------------------------------------


def shelf_pack(sizes: list[tuple[int, int]], padding: int, max_width: int) -> tuple[list[tuple[int, int]], int, int]:
    """Полочная упаковка: детали по убыванию высоты кладутся рядами.

    Простая и предсказуемая: коллайдеры-прямоугольники гарантированно
    не пересекаются, между ними всегда `padding` пустых пикселей.
    """
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    # Ширину подбираем под суммарную площадь, чтобы атлас вышел примерно
    # квадратным, а не полосой в одну деталь.
    total_area = sum(w * h for w, h in sizes)
    target = int((total_area ** 0.5) * 1.15) + padding * 2
    width = min(max(max((w for w, _ in sizes), default=1) + padding * 2, target), max_width)
    pos: list[tuple[int, int]] = [(0, 0)] * len(sizes)
    x = y = shelf_h = 0

    for i in order:
        w, h = sizes[i]
        if x + w + padding > width:
            x = 0
            y += shelf_h + padding
            shelf_h = 0
        pos[i] = (x + padding // 2, y + padding // 2)
        x += w + padding
        shelf_h = max(shelf_h, h)

    total_h = y + shelf_h + padding
    used_w = max((pos[i][0] + sizes[i][0] for i in range(len(sizes))), default=1) + padding
    return pos, int(used_w), int(total_h)


def slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "_" for c in str(text).strip().lower())
    while "__" in keep:
        keep = keep.replace("__", "_")
    return keep.strip("_") or "part"


def pack_atlas(
    parts: Iterable[dict | str | Path],
    out_dir: str | Path,
    device: str = "device",
    padding: int = 6,
    max_width: int = 4096,
    canvas: list[int] | None = None,
    texture_name: str = "texture.png",
) -> dict:
    """Упаковать детали в одну текстуру и создать device.json.

    `parts` — список либо путей к PNG, либо словарей:
      {"file": "...png", "id": "back_cover", "name": "Back Cover",
       "layer": 0, "position": [x,y], "size": [w,h], "rotation": 0}
    Незаданные поля проставляются по умолчанию: layer = порядок в списке,
    position = центр холста, size = натуральный размер детали.
    """
    started = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    spec: list[dict] = []
    for i, item in enumerate(parts):
        if isinstance(item, (str, Path)):
            item = {"file": str(item)}
        f = Path(item["file"])
        if not f.is_file():
            return {"ok": False, "error": "not_found", "message": f"Нет файла детали: {f}"}
        spec.append({**item, "file": f})

    if not spec:
        return {"ok": False, "error": "empty", "message": "Пустой список деталей"}

    images = [Image.open(s["file"]).convert("RGBA") for s in spec]
    sizes = [(im.width, im.height) for im in images]
    pos, atlas_w, atlas_h = shelf_pack(sizes, padding, max_width)

    atlas = Image.new("RGBA", (atlas_w, atlas_h), (0, 0, 0, 0))
    for im, (x, y) in zip(images, pos):
        atlas.alpha_composite(im, (x, y))
    tex_path = out / texture_name
    atlas.save(tex_path, "PNG", compress_level=6)

    if canvas is None:
        cw = int(max(w for w, _ in sizes) * 1.6)
        ch = int(max(h for _, h in sizes) * 1.6)
        canvas = [cw, ch]

    used_ids: set[str] = set()
    parts_json = []
    for i, (s, im, (x, y)) in enumerate(zip(spec, images, pos)):
        pid = slug(s.get("id") or s.get("name") or s["file"].stem)
        base = pid
        k = 2
        while pid in used_ids:
            pid, k = f"{base}_{k}", k + 1
        used_ids.add(pid)

        w, h = im.width, im.height
        size = list(s.get("size") or [w, h])
        position = list(s.get("position") or [canvas[0] / 2, canvas[1] / 2])
        parts_json.append(
            {
                "id": pid,
                "name": s.get("name") or pid.replace("_", " ").title(),
                # общая классификация детали, единая для всех устройств:
                # список типов и правила выбора — в ТИПЫ_ДЕТАЛЕЙ.md в корне
                "type": slug(s.get("type") or "misc"),
                "frame": {"x": int(x), "y": int(y), "w": int(w), "h": int(h)},
                # четыре угла кадра в текстуре: по часовой от левого верхнего
                "corners": [
                    [int(x), int(y)],
                    [int(x + w), int(y)],
                    [int(x + w), int(y + h)],
                    [int(x), int(y + h)],
                ],
                "uv": [
                    round(x / atlas_w, 6),
                    round(y / atlas_h, 6),
                    round((x + w) / atlas_w, 6),
                    round((y + h) / atlas_h, 6),
                ],
                "pivot": [0.5, 0.5],
                "position": [round(float(position[0]), 1), round(float(position[1]), 1)],
                "size": [round(float(size[0]), 1), round(float(size[1]), 1)],
                "scale": float(s.get("scale", 1.0)),
                "rotation": float(s.get("rotation", 0.0)),
                "layer": int(s.get("layer", i)),
                "source": str(s["file"]),
            }
        )

    parts_json.sort(key=lambda p: p["layer"])
    data = {
        "device": device,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "texture": texture_name,
        "texture_size": [atlas_w, atlas_h],
        "canvas": [int(canvas[0]), int(canvas[1])],
        "part_count": len(parts_json),
        "parts": parts_json,
    }
    json_path = out / "device.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "device": device,
        "texture": str(tex_path),
        "texture_size": [atlas_w, atlas_h],
        "json": str(json_path),
        "part_count": len(parts_json),
        "canvas": data["canvas"],
        "elapsed_sec": round(time.time() - started, 2),
        "parts": [{"id": p["id"], "frame": p["frame"], "layer": p["layer"]} for p in parts_json],
        "message": (
            f"Атлас {atlas_w}x{atlas_h}, деталей {len(parts_json)}. "
            "Дальше: правь position/size/rotation/layer в device.json и зови render_assembly."
        ),
    }


# ---------------------------------------------------------------------------
# Шаг 3: отрисовка сборки для визуальной проверки
# ---------------------------------------------------------------------------


def load_device(json_path: str | Path) -> tuple[dict, Path]:
    p = Path(json_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return data, p.parent


def part_image(atlas: Image.Image, part: dict) -> Image.Image:
    f = part["frame"]
    piece = atlas.crop((f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"]))
    w = max(int(round(part["size"][0] * part.get("scale", 1.0))), 1)
    h = max(int(round(part["size"][1] * part.get("scale", 1.0))), 1)
    if (w, h) != piece.size:
        piece = piece.resize((w, h), Image.LANCZOS)
    rot = float(part.get("rotation", 0.0))
    if abs(rot) > 0.01:
        piece = piece.rotate(-rot, resample=Image.BICUBIC, expand=True)
    return piece


def render_assembly(
    json_path: str | Path,
    out: str | Path | None = None,
    mode: str = "flat",
    background: str = "checker",
    labels: bool = False,
    explode_step: int = 60,
    only_layers: list[int] | None = None,
) -> dict:
    """Отрисовать сборку по device.json.

    mode:
      flat     — как выглядит собранное устройство (то, что нужно проверять);
      exploded — детали разнесены по диагонали, видно порядок слоёв;
      grid     — каждая деталь отдельно с подписью id (проверка нарезки).
    background: checker | white | black | transparent
    """
    started = time.time()
    data, root = load_device(json_path)
    atlas = Image.open(root / data["texture"]).convert("RGBA")
    parts = sorted(data["parts"], key=lambda p: p["layer"])
    if only_layers:
        parts = [p for p in parts if p["layer"] in only_layers]
    if not parts:
        return {"ok": False, "error": "empty", "message": "Нет деталей для отрисовки"}

    cw, ch = data.get("canvas", [1024, 1024])

    if mode == "grid":
        cols = min(6, len(parts))
        cell = 260
        rows = (len(parts) + cols - 1) // cols
        canvas = Image.new("RGBA", (cols * cell, rows * (cell + 26)), (30, 30, 34, 255))
        draw = ImageDraw.Draw(canvas)
        font = load_font(16)
        for i, p in enumerate(parts):
            im = part_image(atlas, p)
            im.thumbnail((cell - 16, cell - 16), Image.LANCZOS)
            x, y = (i % cols) * cell, (i // cols) * (cell + 26)
            canvas.alpha_composite(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            draw.text((x + 6, y + cell + 4), f"L{p['layer']} {p['id']}", font=font,
                      fill=(235, 235, 235, 255))
    else:
        pad = explode_step * len(parts) if mode == "exploded" else 0
        canvas = Image.new("RGBA", (int(cw) + pad, int(ch) + pad), (0, 0, 0, 0))
        for i, p in enumerate(parts):
            im = part_image(atlas, p)
            px, py = p["position"]
            if mode == "exploded":
                px += explode_step * i * 0.6
                py += explode_step * i * 0.6
            ox = int(round(px - im.width * p.get("pivot", [0.5, 0.5])[0]))
            oy = int(round(py - im.height * p.get("pivot", [0.5, 0.5])[1]))
            canvas.alpha_composite(im, (ox, oy))

        if labels:
            draw = ImageDraw.Draw(canvas)
            font = load_font(18)
            for p in parts:
                px, py = p["position"]
                draw.text((px, py), f"{p['layer']}:{p['id']}", font=font, fill=(255, 40, 200, 255))

    if background != "transparent":
        if background == "checker":
            bg = Image.new("RGBA", canvas.size, (235, 235, 235, 255))
            d = ImageDraw.Draw(bg)
            for y in range(0, canvas.height, 32):
                for x in range(0, canvas.width, 32):
                    if (x // 32 + y // 32) % 2:
                        d.rectangle([x, y, x + 31, y + 31], fill=(205, 205, 210, 255))
        else:
            fill = (255, 255, 255, 255) if background == "white" else (18, 18, 20, 255)
            bg = Image.new("RGBA", canvas.size, fill)
        canvas = Image.alpha_composite(bg, canvas)

    dst = Path(out) if out else Path(json_path).parent / f"preview_{mode}.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGBA").save(dst, "PNG", compress_level=6)

    return {
        "ok": True,
        "preview": str(dst),
        "mode": mode,
        "size": list(canvas.size),
        "part_count": len(parts),
        "layers": [{"layer": p["layer"], "id": p["id"], "position": p["position"],
                    "size": p["size"], "rotation": p["rotation"]} for p in parts],
        "elapsed_sec": round(time.time() - started, 2),
        "message": f"Превью сохранено: {dst}. Открой его глазами и проверь стыковку.",
    }


def update_parts(json_path: str | Path, updates: list[dict]) -> dict:
    """Точечно поправить детали в device.json.

    updates: [{"id":"battery","position":[x,y],"size":[w,h],"rotation":0,
               "layer":2,"scale":1.0,"name":"Battery","type":"battery"}]
    Передавай только те поля, что меняешь. dx/dy сдвигают позицию относительно.
    """
    data, _ = load_device(json_path)
    by_id = {p["id"]: p for p in data["parts"]}
    changed, missing = [], []

    for u in updates:
        pid = u.get("id")
        p = by_id.get(pid)
        if p is None:
            missing.append(pid)
            continue
        if "dx" in u or "dy" in u:
            p["position"] = [p["position"][0] + float(u.get("dx", 0)),
                             p["position"][1] + float(u.get("dy", 0))]
        for key in ("position", "size", "pivot"):
            if key in u:
                p[key] = [float(v) for v in u[key]]
        for key in ("rotation", "scale"):
            if key in u:
                p[key] = float(u[key])
        if "layer" in u:
            p["layer"] = int(u["layer"])
        if "name" in u:
            p["name"] = u["name"]
        if "type" in u:
            p["type"] = slug(u["type"])
        changed.append(pid)

    data["parts"].sort(key=lambda p: p["layer"])
    Path(json_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": not missing,
        "changed": changed,
        "missing": missing,
        "json": str(json_path),
        "message": (f"Обновлено {len(changed)} деталей"
                    + (f"; не найдены id: {missing}" if missing else "")),
    }


# ---------------------------------------------------------------------------
# Шаг 4: упаковка результата
# ---------------------------------------------------------------------------


PAYLOAD_OPEN = '<script id="asset-payload" type="application/json">'
PAYLOAD_CLOSE = "</script>"


def build_standalone_viewer(
    json_path: str | Path,
    out_html: str | Path | None = None,
    viewer_src: str | Path | None = None,
) -> dict:
    """Собрать HTML, в который вшиты и device.json, и текстура (base64).

    Нужен, потому что страница, открытая по file://, не может прочитать
    соседние файлы: браузер режет fetch/XHR к локальным путям. Поэтому ассет
    кладём внутрь самой страницы — файл открывается двойным кликом и сразу
    показывает сборку, ничего перетаскивать не надо.
    """
    src = Path(viewer_src) if viewer_src else VIEWER_SRC
    if not src.is_file():
        return {"ok": False, "error": "no_viewer", "message": f"Нет шаблона вьювера: {src}"}

    data, root = load_device(json_path)
    tex_file = root / data["texture"]
    if not tex_file.is_file():
        return {"ok": False, "error": "no_texture", "message": f"Нет текстуры: {tex_file}"}

    html = src.read_text(encoding="utf-8")
    start = html.find(PAYLOAD_OPEN)
    if start == -1:
        return {
            "ok": False,
            "error": "no_slot",
            "message": "В шаблоне вьювера нет тега <script id=\"asset-payload\">",
        }
    end = html.find(PAYLOAD_CLOSE, start)

    import base64

    payload = {
        "device": data,
        "texture": "data:image/png;base64," + base64.b64encode(tex_file.read_bytes()).decode(),
    }
    # ensure_ascii=True не нужен, но '<' внутри строки сломал бы разбор тега
    blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")

    out = (
        Path(out_html)
        if out_html
        else root / f"ОТКРЫТЬ_{slug(data.get('device', 'device'))}.html"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        html[: start + len(PAYLOAD_OPEN)] + blob + html[end:],
        encoding="utf-8",
    )
    return {
        "ok": True,
        "file": str(out),
        "bytes": out.stat().st_size,
        "part_count": len(data["parts"]),
        "message": f"Готово: {out.name} — открывается двойным кликом, ассет уже внутри",
    }


def package_asset(
    json_path: str | Path,
    out_dir: str | Path,
    with_viewer: bool = True,
    previews: bool = True,
) -> dict:
    """Сложить готовый ассет в отдельную папку: текстура + json + превью + вьювер."""
    data, root = load_device(json_path)
    dst = Path(out_dir)
    dst.mkdir(parents=True, exist_ok=True)

    shutil.copy2(root / data["texture"], dst / data["texture"])
    (dst / "device.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    made = []
    if previews:
        for mode in ("flat", "exploded"):
            res = render_assembly(dst / "device.json", dst / f"preview_{mode}.png", mode=mode)
            if res["ok"]:
                made.append(res["preview"])

    viewer = standalone = None
    if with_viewer and VIEWER_SRC.is_file():
        shutil.copy2(VIEWER_SRC, dst / "viewer.html")
        viewer = str(dst / "viewer.html")
        name = f"ОТКРЫТЬ_{slug(data.get('device', 'device'))}.html"
        res = build_standalone_viewer(dst / "device.json", dst / name)
        if res["ok"]:
            standalone = res["file"]
        else:
            log(f"Самодостаточный вьювер не собрался: {res['message']}")

    return {
        "ok": True,
        "folder": str(dst),
        "texture": str(dst / data["texture"]),
        "json": str(dst / "device.json"),
        "previews": made,
        "viewer": viewer,
        "open_file": standalone,
        "part_count": len(data["parts"]),
        "message": (
            f"Ассет собран в {dst}. Двойной клик по {Path(standalone).name} — "
            "страница откроется с уже вшитым ассетом, перетаскивать ничего не надо."
            if standalone
            else f"Ассет собран в {dst}."
        ),
    }


def validate_device(json_path: str | Path) -> dict:
    """Проверить device.json: дубли слоёв, вылеты за холст, дубли id, пустые кадры."""
    data, root = load_device(json_path)
    problems: list[str] = []
    cw, ch = data.get("canvas", [0, 0])
    tw, th = data.get("texture_size", [0, 0])

    ids = [p["id"] for p in data["parts"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        problems.append(f"дубли id: {sorted(dupes)}")

    layers = [p["layer"] for p in data["parts"]]
    same = {l for l in layers if layers.count(l) > 1}
    if same:
        problems.append(f"на одном слое несколько деталей: {sorted(same)}")

    for p in data["parts"]:
        f = p["frame"]
        if f["x"] < 0 or f["y"] < 0 or f["x"] + f["w"] > tw or f["y"] + f["h"] > th:
            problems.append(f"{p['id']}: кадр вне текстуры")
        w, h = p["size"][0] * p.get("scale", 1), p["size"][1] * p.get("scale", 1)
        x0, y0 = p["position"][0] - w / 2, p["position"][1] - h / 2
        if x0 < -w or y0 < -h or x0 + w > cw + w or y0 + h > ch + h:
            problems.append(f"{p['id']}: деталь далеко за холстом")

    # пересечения кадров в атласе — упаковка не должна их допускать
    boxes = [(p["id"], p["frame"]) for p in data["parts"]]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][1], boxes[j][1]
            if (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                    and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]):
                problems.append(f"кадры пересекаются в атласе: {boxes[i][0]} / {boxes[j][0]}")

    return {
        "ok": not problems,
        "problems": problems,
        "part_count": len(data["parts"]),
        "canvas": data.get("canvas"),
        "texture_size": data.get("texture_size"),
        "message": "Всё чисто" if not problems else f"Нашлось проблем: {len(problems)}",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Сборка 2D-ассета устройства: атлас + JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="Нарезать детали из PNG с прозрачным фоном")
    e.add_argument("images", nargs="+")
    e.add_argument("--out", required=True)
    e.add_argument("--alpha-thr", type=int, default=40)
    e.add_argument("--min-area-frac", type=float, default=0.0004)
    e.add_argument("--min-side", type=int, default=14)
    e.add_argument("--close-gaps", type=int, default=2)

    h = sub.add_parser("punch", help="Пробить белые внутренние области в прозрачные")
    h.add_argument("files", nargs="+")
    h.add_argument("--tol", type=float, default=42.0)
    h.add_argument("--min-hole-px", type=int, default=24)
    h.add_argument("--suffix", default="", help="Пусто = переписать на месте")
    h.add_argument("--out", help="Папка вывода")

    p = sub.add_parser("pack", help="Упаковать детали в атлас (JSON-список или файлы)")
    p.add_argument("parts", nargs="+", help="PNG-файлы или JSON со списком деталей")
    p.add_argument("--out", required=True)
    p.add_argument("--name", default="device")
    p.add_argument("--padding", type=int, default=6)

    r = sub.add_parser("render", help="Отрисовать сборку по device.json")
    r.add_argument("json")
    r.add_argument("--out")
    r.add_argument("--mode", choices=["flat", "exploded", "grid"], default="flat")
    r.add_argument("--background", choices=["checker", "white", "black", "transparent"],
                   default="checker")
    r.add_argument("--labels", action="store_true")

    k = sub.add_parser("package", help="Сложить готовый ассет в папку")
    k.add_argument("json")
    k.add_argument("--out", required=True)

    v = sub.add_parser("validate", help="Проверить device.json")
    v.add_argument("json")

    s = sub.add_parser("viewer", help="Собрать HTML с вшитым ассетом (двойной клик)")
    s.add_argument("json")
    s.add_argument("--out", help="Путь к html; по умолчанию рядом с device.json")

    args = ap.parse_args()

    if args.cmd == "extract":
        res = extract_parts(args.images, args.out, args.alpha_thr, args.min_area_frac,
                            args.min_side, close_gaps=args.close_gaps)
    elif args.cmd == "punch":
        res = punch_holes(args.files, args.tol, args.min_hole_px, suffix=args.suffix,
                          out_dir=args.out)
    elif args.cmd == "pack":
        items: list = []
        for a in args.parts:
            if a.lower().endswith(".json"):
                blob = json.loads(Path(a).read_text(encoding="utf-8"))
                items += blob["parts"] if isinstance(blob, dict) else blob
            else:
                items += [str(f) for f in expand_inputs([a])]
        res = pack_atlas(items, args.out, args.name, args.padding)
    elif args.cmd == "render":
        res = render_assembly(args.json, args.out, args.mode, args.background, args.labels)
    elif args.cmd == "package":
        res = package_asset(args.json, args.out)
    elif args.cmd == "viewer":
        data, root = load_device(args.json)
        out = args.out or (root / f"ОТКРЫТЬ_{slug(data.get('device', 'device'))}.html")
        res = build_standalone_viewer(args.json, out)
    else:
        res = validate_device(args.json)

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
