#!/usr/bin/env python3
"""Приёмник хуков Claude Code: пишет ленту событий для панели конвейера.

Вызывается из .claude/settings.json:

  session-start  — новая сессия: лента обнуляется, сервер поднимается, окно открывается
  pre            — PreToolUse: инструмент только начал работу (панель показывает «сейчас»)
  post           — PostToolUse: инструмент отработал, в ответе есть пути к файлам
  stop           — Stop: агент отдал ход пользователю, работающих инструментов не осталось

Хук обязан быть незаметным: любая ошибка глушится и код возврата всегда 0 —
сломанная панель не должна мешать работе агента.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
EVENTS = STATE_DIR / "events.jsonl"
# Каждое событие — отдельный файл. Хуки помечены async и запускаются
# параллельно: при дописывании в один общий файл записи в десятки килобайт
# (ответы asset_pack, asset_render) перемешивались между процессами, строка
# переставала быть валидным JSON и молча пропадала — шаг навсегда оставался
# «в работе». Отдельный файл на событие исключает саму гонку.
EVENT_DIR = STATE_DIR / "events.d"
# id текущей сессии. Нужен, потому что сессий может быть открыто несколько:
# старая продолжает писать события в ту же папку и без метки замусоривала бы
# панель новой. Каждое событие помечается своей сессией, сервер показывает
# только последнюю.
SESSION_FILE = STATE_DIR / "session.json"

# Ответы MCP бывают по сотне килобайт (asset_extract) — панели из них нужны
# только пути и пара чисел, поэтому режем.
MAX_RESPONSE = 24000
MAX_INPUT = 6000

# Инструменты, которые панель вообще не интересуют.
SKIP_PREFIX = ("TodoWrite", "Task", "Glob", "Grep", "WebFetch", "WebSearch", "Skill")


def payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def clip(value, limit: int) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text[:limit]


def trim_input(value) -> dict:
    """Оставить вход инструмента, но без гигантских полей (промты, списки деталей)."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        if isinstance(v, str):
            out[k] = v[:MAX_INPUT]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            # список деталей у asset_pack бывает на десятки килобайт: если не
            # влезает — кладём обрезанный текст, пути из него всё равно достанутся
            text = json.dumps(v, ensure_ascii=False)
            out[k] = v if len(text) <= MAX_INPUT else text[:MAX_INPUT]
    return out


def session_id(data: dict) -> str:
    """Чья это сессия. Claude Code кладёт session_id в payload любого хука;
    файл — запасной вариант для событий, где его вдруг не оказалось."""
    sid = data.get("session_id")
    if sid:
        return str(sid)
    try:
        return str(json.loads(SESSION_FILE.read_text(encoding="utf-8")).get("id") or "")
    except Exception:
        return ""


def append(record: dict, session: str = "") -> None:
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    record["ts"] = time.time()
    record["session"] = session
    # имя задаёт порядок при равных ts; pid разводит одновременные хуки
    name = f"{time.time_ns():020d}-{os.getpid()}.json"
    tmp = EVENT_DIR / (name + ".part")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(EVENT_DIR / name)  # сервер увидит файл только целиком


def start_server(open_browser: bool) -> None:
    args = [sys.executable, str(HERE / "server.py"), "--ensure"]
    if open_browser:
        args.append("--open")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.run(args, timeout=20, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, **kwargs)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "post"
    data = payload()

    if mode == "session-start":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(EVENT_DIR, ignore_errors=True)  # каждая сессия — своя лента
        EVENTS.unlink(missing_ok=True)
        sid = str(data.get("session_id") or time.time_ns())
        SESSION_FILE.write_text(json.dumps({"id": sid, "ts": time.time()}),
                                encoding="utf-8")
        append({"event": "SessionStart"}, sid)
        start_server(open_browser=True)
        print(json.dumps({"suppressOutput": True}, ensure_ascii=False))
        return 0

    if mode == "stop":
        # ход закончен: что бы ни осталось «в работе», оно уже отработало
        append({"event": "Stop", "phase": "stop"}, session_id(data))
        return 0

    tool = data.get("tool_name") or ""
    if not tool or tool.startswith(SKIP_PREFIX):
        return 0

    record = {
        "event": "PreToolUse" if mode == "pre" else "PostToolUse",
        "phase": "pre" if mode == "pre" else "post",
        "tool": tool,
        "input": trim_input(data.get("tool_input")),
    }
    if mode != "pre":
        record["response"] = clip(data.get("tool_response") or "", MAX_RESPONSE)
    append(record, session_id(data))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # панель не стоит того, чтобы из-за неё падал хук
        sys.exit(0)
