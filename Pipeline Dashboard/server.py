#!/usr/bin/env python3
"""Живая панель конвейера: HTTP-сервер + сборка состояния из ленты событий.

События пишет hook.py (хуки Claude Code) — сюда попадает каждый вызов
инструмента. Сервер их интерпретирует: раскладывает по шагам конвейера,
собирает галерею картинок и отдаёт всё дашборду одним JSON.

  python server.py                 # запустить сервер в этом окне (порт 7788)
  python server.py --ensure --open # поднять в фоне, если ещё не поднят, и открыть браузер
  python server.py --status        # проверить, жив ли

Дашборд опрашивает /api/state; отдельного пуша нет намеренно — опрос на
localhost стоит копейки, а переживает и падение сервера, и перезапуск.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
STATE_DIR = HERE / "state"
EVENTS = STATE_DIR / "events.jsonl"
EVENT_DIR = STATE_DIR / "events.d"  # по файлу на событие, см. hook.append
PORT = int(os.environ.get("PIPELINE_DASHBOARD_PORT", "7788"))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Шаги конвейера
# ---------------------------------------------------------------------------

STEPS = [
    ("env", "Проверка Flow", "Chrome с отладкой, вкладка проекта, логин"),
    ("burger", "Взрыв-схема", "устройство → изометрическая раскладка слоёв"),
    ("burger_pick", "Выбор схемы", "смотрю 4 варианта глазами, беру лучший"),
    ("layout", "Раскладка деталей", "схема → плоская выкладка деталей лицом"),
    ("bg", "Удаление фона", "белый фон → прозрачность"),
    ("holes", "Пробивка дыр", "карта белых пятен → выбор глазами → заливка"),
    ("extract", "Нарезка деталей", "связные области → отдельные PNG + контактка"),
    ("pick", "Выбор деталей", "комплект по контактке, мусор в сторону"),
    ("punch", "Вырезы в деталях", "сквозные отверстия у корпусов"),
    ("shrink", "Срез каймы", "минус 2 px по всей границе с прозрачностью"),
    ("pack", "Атлас", "детали → texture.png + device.json"),
    ("assemble", "Сборка", "рендер → правка позиций → рендер, по кругу"),
    ("package", "Упаковка", "валидация и готовая папка ассета"),
]

# Инструмент -> шаг. flow_generate уточняется по тексту промта.
TOOL_STEP = {
    "mcp__flow-images__flow_check": "env",
    "mcp__flow-images__flow_setup": "env",
    "mcp__bg-remover__bg_check": "bg",
    "mcp__bg-remover__bg_remove": "bg",
    "mcp__bg-remover__bg_inspect": "holes",
    "mcp__bg-remover__bg_punch": "holes",
    "mcp__bg-remover__bg_shrink": "shrink",
    "mcp__asset-builder__asset_extract": "extract",
    "mcp__asset-builder__asset_punch": "punch",
    "mcp__asset-builder__asset_pack": "pack",
    "mcp__asset-builder__asset_render": "assemble",
    "mcp__asset-builder__asset_update": "assemble",
    "mcp__asset-builder__asset_validate": "package",
    "mcp__asset-builder__asset_package": "package",
    "mcp__asset-builder__asset_viewer": "package",
}

# Что показывать в строке «сейчас» вместо сырого имени инструмента.
TOOL_LABEL = {
    "mcp__flow-images__flow_check": "проверяю Google Flow",
    "mcp__flow-images__flow_generate": "генерирую картинки во Flow",
    "mcp__bg-remover__bg_remove": "вырезаю фон",
    "mcp__bg-remover__bg_inspect": "ищу непробитые белые дыры",
    "mcp__bg-remover__bg_punch": "пробиваю дыры по меткам",
    "mcp__bg-remover__bg_shrink": "срезаю белую кайму",
    "mcp__asset-builder__asset_extract": "нарезаю детали",
    "mcp__asset-builder__asset_punch": "пробиваю вырезы в деталях",
    "mcp__asset-builder__asset_pack": "собираю атлас",
    "mcp__asset-builder__asset_render": "рендерю сборку",
    "mcp__asset-builder__asset_update": "двигаю детали",
    "mcp__asset-builder__asset_validate": "проверяю ассет",
    "mcp__asset-builder__asset_package": "упаковываю ассет",
    "Read": "смотрю картинку глазами",
}

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

# Скрипт refine.py зовётся из оболочки — распознаём по подкоманде.
SHELL_STEP = [
    (re.compile(r"refine\.py[^|<>]*\binspect\b", re.I), "holes", "ищу непробитые белые дыры"),
    (re.compile(r"refine\.py[^|<>]*\bpunch\b", re.I), "holes", "пробиваю дыры по меткам"),
    (re.compile(r"refine\.py[^|<>]*\bshrink\b", re.I), "shrink", "срезаю белую кайму"),
    (re.compile(r"bg_remove\.py", re.I), "bg", "вырезаю фон"),
]


# ---------------------------------------------------------------------------
# Лента событий
# ---------------------------------------------------------------------------


def read_events() -> list[dict]:
    """Лента: по файлу на событие (events.d) плюс старый общий файл, если остался."""
    out = []
    if EVENTS.is_file():
        with EVENTS.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if EVENT_DIR.is_dir():
        for f in sorted(EVENT_DIR.glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8", errors="replace")))
            except (OSError, json.JSONDecodeError):
                continue
    out.sort(key=lambda e: float(e.get("ts") or 0))
    return out


def prompt_step(prompt: str) -> tuple[str, str]:
    """Какой это из двух промтов конвейера — по близости к файлам в Promts/.

    Раньше здесь искалось слово «flat-lay», но промты пользователь правит
    руками, и любое такое слово может исчезнуть. Сравнение с самими файлами
    переживает переписывание текста.
    """
    words = set(re.sub(r"\s+", " ", prompt.lower()).split())
    best, score = None, 0.0
    for f in sorted((PROJECT / "Promts").glob("*.txt")) if (PROJECT / "Promts").is_dir() else []:
        try:
            other = set(re.sub(r"\s+", " ", f.read_text(encoding="utf-8").lower()).split())
        except OSError:
            continue
        overlap = len(words & other) / max(len(words | other), 1)
        if overlap > score:
            best, score = f.name, overlap
    if best and score >= 0.3:
        return ("layout", "генерирую раскладку деталей") if best.startswith("3") \
            else ("burger", "генерирую взрыв-схему")
    # промт не из файлов — шаг угадаем по наличию слова про раскладку
    low = prompt.lower()
    if "flat-lay" in low or "parts layout" in low:
        return "layout", "генерирую раскладку деталей"
    return "burger", "генерирую взрыв-схему"


def stage_of(ev: dict) -> tuple[str | None, str]:
    """(шаг конвейера, человекочитаемое действие) для события."""
    tool = ev.get("tool") or ""
    inp = ev.get("input") or {}

    if tool == "mcp__flow-images__flow_generate":
        return prompt_step(str(inp.get("prompt", "")))

    if tool in ("Bash", "PowerShell"):
        cmd = str(inp.get("command", ""))
        for rx, step, label in SHELL_STEP:
            if rx.search(cmd):
                return step, label
        return None, ""

    if tool == "Read":
        path = str(inp.get("file_path", "")).lower()
        if not path.endswith(IMG_EXT):
            return None, ""
        if "contact_sheet" in path or "\\parts\\" in path:
            return "pick", "разглядываю контактку деталей"
        if "preview_" in path or "\\build\\" in path:
            return "assemble", "проверяю сборку глазами"
        if "_holes" in path:
            return "holes", "разглядываю карту дыр"
        if "\\burger\\" in path:
            return "burger_pick", "сравниваю варианты схемы"
        return "layout", "смотрю раскладку глазами"

    return TOOL_STEP.get(tool), TOOL_LABEL.get(tool, "")


def find_paths(text: str) -> list[str]:
    """Вытащить пути к картинкам из ответа инструмента."""
    if not text:
        return []
    found = re.findall(r"[A-Za-z]:\\\\?[^\"'\r\n]*?\.(?:png|jpe?g|webp)", text)
    out = []
    for p in found:
        p = p.replace("\\\\", "\\").strip()
        if p not in out:
            out.append(p)
    return out


def load_marks(image_path: str) -> list[dict]:
    """Метки дыр из <имя>_holes.json, если карта уже посчитана."""
    p = Path(image_path)
    cand = p.parent / (p.stem + "_holes.json")
    if not cand.is_file():
        return []
    try:
        data = json.loads(cand.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    w, h = data.get("image_size", [0, 0]) or [0, 0]
    if not w or not h:
        return []
    marks = []
    for r in data.get("regions", [])[:80]:
        x, y = r.get("seed", [0, 0])
        marks.append(
            {
                "id": r.get("id"),
                "x": round(100.0 * x / w, 3),
                "y": round(100.0 * y / h, 3),
                "enclosed": bool(r.get("enclosed")),
            }
        )
    return marks


class Gallery:
    """Картинки конвейера: одна карточка на файл, стадия обновляется по ходу."""

    ORDER = {"generated": 0, "scan": 1, "cut": 2, "marked": 3, "punched": 4, "parts": 5, "build": 6}

    def __init__(self):
        self.items: dict[str, dict] = {}

    def touch(self, path: str, stage: str, ts: float, note: str = "", marks=None):
        if not path:
            return
        key = path.lower()
        it = self.items.get(key)
        if it is None:
            it = {"path": path, "name": Path(path).name, "stage": stage, "ts": ts, "note": note,
                  "marks": marks or [], "hot": False}
            self.items[key] = it
            return
        # стадия только вперёд: повторный Read не должен откатывать «вырезано» в «смотрю»
        if self.ORDER.get(stage, 0) >= self.ORDER.get(it["stage"], 0):
            it["stage"] = stage
        it["ts"] = ts
        if note:
            it["note"] = note
        if marks:
            it["marks"] = marks

    def hot(self, paths: list[str]):
        for it in self.items.values():
            it["hot"] = False
        for p in paths:
            it = self.items.get(p.lower())
            if it:
                it["hot"] = True

    def list(self) -> list[dict]:
        return sorted(self.items.values(), key=lambda i: i["ts"])


def active_session(events: list[dict]) -> tuple[str, list[dict]]:
    """Показываем только последнюю сессию.

    Сессий может быть открыто несколько сразу: старая продолжает слать события
    в ту же папку и без этого фильтра дописывалась бы в панель новой, а панель
    новой не начиналась бы с чистого листа. Режем по последнему SessionStart и
    выкидываем всё, что помечено чужой сессией.
    """
    start_at, sid = 0, ""
    for i, ev in enumerate(events):
        if ev.get("event") == "SessionStart":
            start_at, sid = i, str(ev.get("session") or "")
    tail = events[start_at:]
    if sid:
        tail = [e for e in tail if str(e.get("session") or sid) == sid]
    return sid, tail


def build_state() -> dict:
    session, events = active_session(read_events())
    steps = {k: {"key": k, "title": t, "hint": h, "status": "pending", "count": 0}
             for k, t, h in STEPS}
    order = [k for k, _, _ in STEPS]

    gallery = Gallery()
    feed: list[dict] = []
    device = None
    current = None
    started = None
    last_ts = 0.0
    assemble_iters = 0
    parts_total = 0

    def close_current():
        """Инструмент, начатый раньше, точно отработал — не держать шаг «в работе».

        Своё post-событие может не дойти (хук async, Claude Code его не
        гарантирует), поэтому висящий вызов закрывает любое следующее событие
        ленты и конец хода.
        """
        nonlocal current
        if current:
            steps[current["step"]]["status"] = "done"
            current = None

    for ev in events:
        ts = float(ev.get("ts") or 0)
        last_ts = max(last_ts, ts)
        kind = ev.get("event")

        if kind == "SessionStart":
            started = ts
            continue

        if kind == "Stop":
            close_current()
            continue

        tool = ev.get("tool") or ""
        inp = ev.get("input") or {}
        resp = ev.get("response") or ""
        step, label = stage_of(ev)
        # закрываем висящий вызов даже событием не из конвейера: раз агент
        # успел взяться за что-то другое, предыдущий инструмент уже вернулся
        if current and not (ev.get("phase") == "post" and current.get("tool") == tool):
            close_current()
        if not step:
            continue

        steps[step]["count"] += 1
        if ev.get("phase") == "pre":
            current = {"step": step, "label": label or tool, "tool": tool, "ts": ts}
            steps[step]["status"] = "active"
            # подсветить картинки, с которыми инструмент работает прямо сейчас
            gallery.hot(find_paths(json.dumps(inp, ensure_ascii=False)))
            continue

        # phase == post
        steps[step]["status"] = "done"
        current = None
        gallery.hot([])

        # имя устройства: из ответа asset_pack либо из пути work\<device>\...
        for m in re.finditer(r'"device"\s*:\s*"([^"]+)"', resp):
            device = m.group(1)
        if not device:
            probe = json.dumps(inp, ensure_ascii=False) + resp[:4000]
            m = re.search(r"work\\{1,2}([^\\\"/]+)\\", probe)
            if m:
                device = m.group(1)

        out_paths = find_paths(resp)
        in_paths = find_paths(json.dumps(inp, ensure_ascii=False))

        if tool == "mcp__flow-images__flow_generate":
            for p in out_paths:
                gallery.touch(p, "generated", ts, "сгенерировано во Flow")
        elif tool == "Read":
            for p in in_paths:
                gallery.touch(p, "scan", ts, "просмотрено")
        elif tool == "mcp__bg-remover__bg_remove":
            for p in out_paths:
                gallery.touch(p, "cut", ts, "фон удалён")
        elif step == "holes":
            target = (in_paths or out_paths)[:1]
            for p in target:
                base = p.replace("_holes.png", ".png")
                marks = load_marks(base)
                stage = "marked" if "inspect" in (label or "") or "ищу" in (label or "") else "punched"
                gallery.touch(base, stage, ts,
                              f"меток: {len(marks)}" if stage == "marked" else "дыры пробиты",
                              marks)
        elif tool == "mcp__asset-builder__asset_extract":
            m = re.search(r'"count"\s*:\s*(\d+)', resp)
            parts_total = int(m.group(1)) if m else parts_total
            for p in out_paths:
                if "contact_sheet" in p.lower():
                    gallery.touch(p, "parts", ts, f"деталей: {parts_total}")
        elif tool == "mcp__asset-builder__asset_render":
            assemble_iters += 1
            for p in out_paths:
                if "preview" in p.lower():
                    gallery.touch(p, "build", ts, f"итерация {assemble_iters}")
        elif tool in ("mcp__asset-builder__asset_package", "mcp__asset-builder__asset_pack"):
            for p in out_paths:
                if "preview" in p.lower():
                    gallery.touch(p, "build", ts, "готовый ассет")

        feed.append({
            "ts": ts,
            "step": step,
            "text": label or tool,
            "detail": summarize(tool, inp, resp),
        })

    # последний рубеж: даже самая долгая генерация во Flow укладывается
    # в gen_timeout=600 c, так что «в работе» дольше — потерянное событие
    if current and time.time() - float(current.get("ts") or 0) > 900:
        close_current()

    # шаги до последнего сделанного, но без событий, помечаем пропущенными
    done_idx = [order.index(k) for k, s in steps.items() if s["status"] == "done"]
    frontier = max(done_idx) if done_idx else -1
    for i, k in enumerate(order):
        if steps[k]["status"] == "pending" and i < frontier:
            steps[k]["status"] = "skipped"

    done = sum(1 for s in steps.values() if s["status"] == "done")
    return {
        "version": len(events),
        "session": session,
        "now": time.time(),
        "started": started,
        "last_ts": last_ts,
        "device": device,
        "steps": [steps[k] for k in order],
        "progress": round(100.0 * done / len(order), 1),
        "done": done,
        "total": len(order),
        "current": current,
        "images": gallery.list(),
        "feed": feed[-40:],
        "parts_total": parts_total,
        "assemble_iters": assemble_iters,
    }


def summarize(tool: str, inp: dict, resp: str) -> str:
    """Короткая строка с сутью результата — то, что стоит видеть в ленте."""
    bits = []
    for key, fmt in (
        ("downloaded", "картинок: {}"),
        ("count", "найдено: {}"),
        ("punched", "пробито: {}"),
        ("done", "готово: {}"),
        ("part_count", "деталей: {}"),
        ("removed_px", "срезано px: {}"),
    ):
        m = re.search(r'"%s"\s*:\s*(\d+)' % key, resp)
        if m:
            bits.append(fmt.format(m.group(1)))
    m = re.search(r'"opaque_share"\s*:\s*([\d.]+)', resp)
    if m:
        bits.append(f"объект {float(m.group(1)) * 100:.0f}% кадра")
    if '"ok": false' in resp or '"ok":false' in resp:
        bits.append("ОШИБКА")
    if tool == "Read" and inp.get("file_path"):
        bits.append(Path(str(inp["file_path"])).name)
    return " · ".join(bits[:4])


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_thumb_cache: dict[tuple, bytes] = {}


def thumbnail(path: Path, width: int) -> bytes | None:
    """PNG-превью нужной ширины. Кеш по (путь, mtime, ширина)."""
    try:
        key = (str(path).lower(), path.stat().st_mtime_ns, width)
    except OSError:
        return None
    hit = _thumb_cache.get(key)
    if hit is not None:
        return hit
    try:
        from PIL import Image

        im = Image.open(path)
        im = im.convert("RGBA") if im.mode not in ("RGB", "RGBA") else im
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=3)
        data = buf.getvalue()
    except Exception:
        return None
    if len(_thumb_cache) > 200:
        _thumb_cache.clear()
    _thumb_cache[key] = data
    return data


# ---------------------------------------------------------------------------
# Запись device.json из вьювера
# ---------------------------------------------------------------------------
#
# Вьювер, открытый двойным кликом по ОТКРЫТЬ_<device>.html, живёт на file:// и
# сам писать на диск не может — браузер даёт только «Сохранить как» с окном
# проводника. Поэтому кнопка «Сохранить» шлёт JSON сюда, а файл на диск кладёт
# уже сервер, ровно по тому пути, из которого ассет и был вшит.

SAVE_DIRS = (PROJECT / "assets", PROJECT / "work")


def save_device_json(raw_path: str, device: dict) -> dict:
    """Записать device.json по абсолютному пути внутри проекта."""
    if not isinstance(device, dict) or not device.get("parts"):
        return {"ok": False, "error": "bad_device", "message": "в теле нет parts"}
    try:
        path = Path(raw_path).resolve()
    except OSError:
        return {"ok": False, "error": "bad_path", "message": "путь не разобрался"}
    if path.suffix.lower() != ".json":
        return {"ok": False, "error": "not_json", "message": "писать можно только .json"}
    # наружу проекта не пишем никогда, даже по прямой просьбе страницы
    if not any(_inside(path, d) for d in SAVE_DIRS):
        return {"ok": False, "error": "outside", "message": f"путь вне assets/ и work/: {path}"}
    if not path.is_file():
        return {"ok": False, "error": "no_file", "message": f"файла нет: {path}"}

    path.write_text(json.dumps(device, ensure_ascii=False, indent=2), encoding="utf-8")

    # ассет вшит в ОТКРЫТЬ_*.html копией — пересобираем, иначе следующее
    # открытие покажет старую сборку
    viewer = None
    try:
        sys.path.insert(0, str(PROJECT / "Asset Builder MCP"))
        import asset_builder as ab  # noqa: PLC0415

        name = f"ОТКРЫТЬ_{ab.slug(device.get('device', 'device'))}.html"
        res = ab.build_standalone_viewer(path, path.parent / name)
        if res.get("ok"):
            viewer = res.get("file")
    except Exception as exc:  # пересборка не критична, сам JSON уже записан
        viewer = f"не пересобрал: {exc}"

    return {"ok": True, "path": str(path), "viewer": viewer}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # тишина: сервер живёт в фоне
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        # вьювер приходит с file:// (Origin: null) — без этого браузер режет ответ
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Vary", "Origin")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            html = (HERE / "dashboard.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        if route == "/api/state":
            body = json.dumps(build_state(), ensure_ascii=False).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")

        if route == "/api/ping":
            return self._send(200, b'{"ok":true}', "application/json")

        if route == "/img":
            raw = (qs.get("p") or [""])[0]
            width = min(max(int((qs.get("w") or ["520"])[0]), 32), 2000)
            path = Path(urllib.parse.unquote(raw))
            # наружу отдаём только то, что лежит внутри проекта
            try:
                path.resolve().relative_to(PROJECT.resolve())
            except (ValueError, OSError):
                return self._send(403, b"outside project", "text/plain; charset=utf-8")
            data = thumbnail(path, width)
            if data is None:
                return self._send(404, b"no image", "text/plain; charset=utf-8")
            return self._send(200, data, "image/png", cache="max-age=30")

        self._send(404, b"not found", "text/plain; charset=utf-8")

    # Записывать даём только своим: страница с file:// шлёт Origin: null,
    # дашборд — http://127.0.0.1. Чужой сайт из браузера сюда не достучится.
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        return origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if route != "/api/save-device":
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        if not self._origin_ok():
            return self._send(403, b'{"ok":false,"error":"origin"}', "application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            res = save_device_json(payload.get("path", ""), payload.get("device"))
        except Exception as exc:
            res = {"ok": False, "error": "crash", "message": str(exc)}
        body = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self._send(200 if res.get("ok") else 400, body, "application/json; charset=utf-8")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def alive(port: int = PORT, timeout: float = 0.6) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout):
            return True
    except Exception:
        return False


def spawn_detached() -> None:
    """Поднять сервер отдельным процессом, переживающим выход из хука."""
    flags = 0
    kwargs = {}
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    exe = sys.executable
    # pythonw не держит консоль — на Windows это важно, иначе мигает окно
    if os.name == "nt":
        cand = Path(exe).with_name("pythonw.exe")
        if cand.is_file():
            exe = str(cand)
    subprocess.Popen(
        [exe, str(HERE / "server.py"), "--serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        cwd=str(HERE), **kwargs,
    )


def serve() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"[{datetime.now():%H:%M:%S}] панель на http://127.0.0.1:{PORT}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Живая панель конвейера device-generator")
    ap.add_argument("--serve", action="store_true", help="работать в этом процессе")
    ap.add_argument("--ensure", action="store_true", help="поднять в фоне, если не поднят")
    ap.add_argument("--open", action="store_true", help="открыть браузер")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        print(json.dumps({"alive": alive(), "port": PORT, "events": str(EVENTS)}, ensure_ascii=False))
        return 0

    if args.ensure:
        if not alive():
            spawn_detached()
            for _ in range(40):  # ~4 с на подъём
                if alive(timeout=0.25):
                    break
                time.sleep(0.1)
        if args.open and alive():
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return 0 if alive() else 1

    if args.open:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return 0

    return serve()


if __name__ == "__main__":
    sys.exit(main())
