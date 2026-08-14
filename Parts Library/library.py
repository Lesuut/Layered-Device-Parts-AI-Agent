#!/usr/bin/env python3
"""Shared warehouse of cut-out parts.

Every part asset_extract has ever produced ends up here — the ones that made it
into an atlas and the ones that were thrown away. The point is to avoid
regenerating: a missing part is often already sitting here from an earlier
device.

Layout:

  Parts Library/
    index.json            registry: sha1 -> part record
    used/, unused/        the pieces themselves, named <device>_<source>_<sha8>.png
    _contact_used_NN.png  contact sheets for the eye, every part labelled
    _contact_unused_NN.png

Commands:

  py library.py scan                     walk work/ and assets/, pull in everything found
  py library.py ingest <dir> [device]    pull in one folder of cut parts
  py library.py mark <device.json>       mark the parts that went into a finished asset
  py library.py sheet [device]           redraw the contact sheets
  py library.py hook                     PostToolUse mode: decides from the tool call
  py library.py stats                    what is in the warehouse

Deduplicated by content sha1: extracting the same image twice does not grow the
warehouse, the record just collects another origin.

The hook must stay invisible: every error is swallowed, exit code is always 0.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
INDEX = HERE / "index.json"

# The Windows console defaults to cp1252 — non-ASCII output kills it.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WORK = ROOT / "work"
ASSETS = ROOT / "assets"

# The warehouse is flat: everything lives in two shared folders rather than one
# folder per device, so it can all be flipped through at once. Where a part came
# from is visible in its file name (it starts with the device) and in index.json.
USED_DIR = "used"
UNUSED_DIR = "unused"

# Contact sheet: cell size, cells per row and rows per page — without paging,
# 600 parts produce a PNG nothing can open.
CELL = 210
COLS = 8
PAD = 10
ROWS_PER_SHEET = 12


# ---------------------------------------------------------------- index

def load_index() -> dict:
    if not INDEX.exists():
        return {"updated": None, "parts": {}}
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:
        return {"updated": None, "parts": {}}


def save_index(data: dict) -> None:
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    INDEX.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def device_of(path: Path) -> str:
    """Device name = the segment right after work/ or assets/."""
    parts = [p.lower() for p in path.parts]
    for anchor in ("work", "assets"):
        if anchor in parts:
            i = parts.index(anchor)
            if i + 1 < len(path.parts):
                return path.parts[i + 1]
    return "misc"


def png_size(path: Path):
    try:
        from PIL import Image

        with Image.open(path) as im:
            return list(im.size)
    except Exception:
        return None


# ---------------------------------------------------------------- ingest

def is_part_file(path: Path) -> bool:
    if path.suffix.lower() != ".png":
        return False
    name = path.name.lower()
    if name.startswith("contact_sheet") or name.startswith("_contact"):
        return False
    # Atlases, previews and intermediate images that still have a background
    # are not parts.
    if name in ("texture.png",) or name.startswith("preview_"):
        return False
    if name.endswith("_nobg.png") or name.endswith("_holes.png"):
        return False
    return True


def ingest_file(path: Path, index: dict, device: str | None = None) -> str | None:
    """Put one part into the warehouse. Returns its sha1, or None."""
    if not path.is_file() or not is_part_file(path):
        return None
    dev = device or device_of(path)
    try:
        digest = sha1_of(path)
    except Exception:
        return None

    rec = index["parts"].get(digest)
    origin = str(path.relative_to(ROOT)) if str(path).startswith(str(ROOT)) else str(path)

    if rec:
        if origin not in rec["origins"]:
            rec["origins"].append(origin)
        return digest

    dest_dir = HERE / UNUSED_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{dev}_{path.stem}_{digest[:8]}.png"
    if not dest.exists():
        shutil.copy2(path, dest)

    index["parts"][digest] = {
        "file": f"{UNUSED_DIR}/{dest.name}",
        "device": dev,
        "size": png_size(path),
        "origins": [origin],
        "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "used": False,
        "used_in": [],
        "id": None,
        "type": None,
    }
    return digest


def ingest_dir(directory: Path, device: str | None = None) -> int:
    index = load_index()
    before = len(index["parts"])
    for path in sorted(directory.rglob("*.png")):
        ingest_file(path, index, device)
    save_index(index)
    return len(index["parts"]) - before


# ---------------------------------------------------------------- sorting into the two folders

def place(rec: dict) -> None:
    """Move a part's file into the folder its fate calls for: used or unused."""
    want = USED_DIR if rec.get("used") else UNUSED_DIR
    current = Path(rec["file"])
    # The name has to start with the device: in one flat pile that is the only
    # clue where a part came from. The old per-device layout did not need it.
    name = current.name
    device = rec.get("device") or "misc"
    if not name.startswith(f"{device}_"):
        name = f"{device}_{name}"
    if current.parent.name == want and name == current.name:
        return
    src = HERE / current
    dst_dir = HERE / want
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    try:
        if src.exists():
            if dst.exists():
                src.unlink()
            else:
                shutil.move(str(src), str(dst))
        rec["file"] = f"{want}/{name}"
    except Exception:
        pass


def place_all(index: dict) -> None:
    for rec in index["parts"].values():
        place(rec)


# ---------------------------------------------------------------- marking "went into an asset"

def mark_used_files(files: list[str], device_json: str | None = None) -> int:
    """Exact marking: we know the file paths the atlas was packed from."""
    index = load_index()
    by_origin = {}
    for digest, rec in index["parts"].items():
        for origin in rec["origins"]:
            by_origin[Path(origin).name] = digest
            by_origin[origin.replace("\\", "/")] = digest

    marked = 0
    for raw in files:
        path = Path(raw)
        digest = None
        try:
            rel = str(path.resolve().relative_to(ROOT)).replace("\\", "/")
            digest = by_origin.get(rel)
        except Exception:
            pass
        if digest is None and path.is_file():
            # Not in the registry yet (a fresh cut) — pull it in on the spot.
            digest = ingest_file(path, index)
        if digest is None:
            digest = by_origin.get(path.name)
        if digest is None:
            continue
        rec = index["parts"][digest]
        rec["used"] = True
        if device_json and device_json not in rec["used_in"]:
            rec["used_in"].append(device_json)
        place(rec)
        marked += 1
    save_index(index)
    return marked


def mark_used_json(device_json: Path) -> int:
    """Marking after the fact: a finished asset carries no file names, but
    frame.w/h match the natural size of the part that was cut."""
    try:
        data = json.loads(device_json.read_text(encoding="utf-8"))
    except Exception:
        return 0
    dev = data.get("device") or device_of(device_json)
    index = load_index()
    marked = 0
    rel = str(device_json.relative_to(ROOT)) if str(device_json).startswith(str(ROOT)) else str(device_json)

    for part in data.get("parts", []):
        frame = part.get("frame") or {}
        wh = [frame.get("w"), frame.get("h")]
        if None in wh:
            continue
        for rec in index["parts"].values():
            if rec["device"] != dev or rec.get("size") != wh:
                continue
            rec["used"] = True
            rec["id"] = rec["id"] or part.get("id")
            rec["type"] = rec["type"] or part.get("type")
            if rel not in rec["used_in"]:
                rec["used_in"].append(rel)
            place(rec)
            marked += 1
    save_index(index)
    return marked


# ---------------------------------------------------------------- contact sheet

def build_sheet(bucket: str) -> list[Path]:
    """Contact sheet for one pile, paged. bucket is USED_DIR or UNUSED_DIR."""
    from PIL import Image, ImageDraw

    index = load_index()
    want_used = bucket == USED_DIR
    recs = sorted(
        (r for r in index["parts"].values() if bool(r["used"]) == want_used),
        key=lambda r: (r["device"], r["file"]),
    )
    for old in HERE.glob(f"_contact_{bucket}*.png"):
        old.unlink(missing_ok=True)
    if not recs:
        return []

    per_page = COLS * ROWS_PER_SHEET
    pages = (len(recs) + per_page - 1) // per_page
    out_paths: list[Path] = []

    for page in range(pages):
        chunk = recs[page * per_page : (page + 1) * per_page]
        rows = (len(chunk) + COLS - 1) // COLS
        sheet = Image.new("RGB", (COLS * CELL, rows * (CELL + 26)), (26, 28, 32))
        draw = ImageDraw.Draw(sheet)

        for i, rec in enumerate(chunk):
            cx = (i % COLS) * CELL
            cy = (i // COLS) * (CELL + 26)
            try:
                with Image.open(HERE / rec["file"]) as im:
                    im = im.convert("RGBA")
                    im.thumbnail((CELL - 2 * PAD, CELL - 2 * PAD), Image.LANCZOS)
                    sheet.paste(im, (cx + (CELL - im.width) // 2, cy + (CELL - im.height) // 2), im)
            except Exception:
                continue
            colour = (120, 210, 130) if rec["used"] else (170, 170, 178)
            size = rec.get("size") or ["?", "?"]
            label = Path(rec["file"]).stem.rsplit("_", 1)[0]
            draw.text((cx + 6, cy + CELL + 4), f"{label} {size[0]}x{size[1]}", fill=colour)

        suffix = "" if pages == 1 else f"_{page + 1:02d}"
        out = HERE / f"_contact_{bucket}{suffix}.png"
        sheet.save(out)
        out_paths.append(out)

    return out_paths


def build_all_sheets() -> list[Path]:
    return build_sheet(USED_DIR) + build_sheet(UNUSED_DIR)


# ---------------------------------------------------------------- walking the whole project

def scan_all() -> dict:
    index = load_index()
    before = len(index["parts"])
    for base in (WORK, ASSETS):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.png")):
            # Parts live in cut folders: parts, parts_raw, parts_b, parts_disc…
            if not any(seg.startswith("parts") for seg in path.parts):
                continue
            ingest_file(path, index)
    save_index(index)

    # Mark everything that already went into a finished asset.
    for device_json in sorted(ASSETS.rglob("device.json")):
        mark_used_json(device_json)

    index = load_index()
    place_all(index)
    save_index(index)
    cleanup_old_layout()
    return {"added": len(index["parts"]) - before, "total": len(index["parts"])}


def cleanup_old_layout() -> None:
    """The warehouse used to be split per device. Drop the emptied folders."""
    keep = {USED_DIR, UNUSED_DIR}
    for path in HERE.iterdir():
        if not path.is_dir() or path.name in keep:
            continue
        leftovers = list(path.rglob("*"))
        if not leftovers:
            path.rmdir()


# ---------------------------------------------------------------- statistics

def stats() -> dict:
    index = load_index()
    per = {}
    for rec in index["parts"].values():
        slot = per.setdefault(rec["device"], {"total": 0, "used": 0})
        slot["total"] += 1
        slot["used"] += 1 if rec["used"] else 0
    return {"devices": per, "total": len(index["parts"])}


# ---------------------------------------------------------------- hook

def hook() -> None:
    """PostToolUse: works out for itself which tool just ran."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return

    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}

    if tool.endswith("asset_extract"):
        out_dir = tool_input.get("out_dir")
        if out_dir and Path(out_dir).exists():
            ingest_dir(Path(out_dir))
            build_sheet(UNUSED_DIR)
        return

    if tool.endswith("asset_pack"):
        files = [p.get("file") for p in (tool_input.get("parts") or []) if p.get("file")]
        out_dir = tool_input.get("out_dir")
        target = str(Path(out_dir) / "device.json") if out_dir else None
        if files:
            mark_used_files(files, target)
            index = load_index()
            # The call itself carries each part's id and type — record them.
            by_name = {}
            for digest, rec in index["parts"].items():
                for origin in rec["origins"]:
                    by_name.setdefault(Path(origin).name, digest)
            for spec in tool_input.get("parts") or []:
                digest = by_name.get(Path(spec.get("file", "")).name)
                if digest:
                    index["parts"][digest]["id"] = spec.get("id") or index["parts"][digest]["id"]
                    index["parts"][digest]["type"] = spec.get("type") or index["parts"][digest]["type"]
            save_index(index)
            build_all_sheets()
        return


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "stats"

    if cmd == "hook":
        try:
            hook()
        except Exception:
            pass
        return 0

    if cmd == "scan":
        result = scan_all()
        sheets = build_all_sheets()
        print(json.dumps({**result, "sheets": [s.name for s in sheets]}, ensure_ascii=False, indent=2))
        return 0

    if cmd == "ingest":
        directory = Path(argv[2])
        device = argv[3] if len(argv) > 3 else None
        added = ingest_dir(directory, device)
        build_sheet(UNUSED_DIR)
        print(json.dumps({"added": added}, ensure_ascii=False))
        return 0

    if cmd == "rebuild":
        # Clear every mark and sort again from the finished assets.
        index = load_index()
        for rec in index["parts"].values():
            rec["used"] = False
            rec["used_in"] = []
        save_index(index)
        result = scan_all()
        build_all_sheets()
        print(json.dumps({**result, **stats()}, ensure_ascii=False, indent=2))
        return 0

    if cmd == "mark":
        marked = mark_used_json(Path(argv[2]))
        build_all_sheets()
        print(json.dumps({"marked": marked}, ensure_ascii=False))
        return 0

    if cmd == "sheet":
        for path in build_all_sheets():
            print(path.name)
        return 0

    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
