#!/usr/bin/env python3
"""Claude Code hook sink: writes the event feed for the pipeline dashboard.

Invoked from .claude/settings.json:

  session-start  — new session: feed cleared, server started, window opened
  pre            — PreToolUse: a tool just started (the dashboard shows it as "now")
  post           — PostToolUse: a tool finished, its response carries file paths
  stop           — Stop: the agent handed the turn back, nothing is running

The hook must stay invisible: every error is swallowed and the exit code is
always 0 — a broken dashboard must not get in the agent's way.
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
# One file per event. The hooks are marked async and run in parallel: appending
# to one shared file let records tens of kilobytes long (asset_pack and
# asset_render responses) interleave between processes — the line stopped being
# valid JSON and vanished silently, leaving a step stuck "in progress" forever.
# A file per event removes the race entirely.
EVENT_DIR = STATE_DIR / "events.d"
# Current session id. Needed because several sessions can be open at once: the
# old one keeps writing events into the same folder and, unlabelled, would
# clutter the new one's dashboard. Every event carries its session, the server
# shows only the latest.
SESSION_FILE = STATE_DIR / "session.json"

# MCP responses run to a hundred kilobytes (asset_extract) — the dashboard only
# needs the paths and a couple of numbers, so they get clipped.
MAX_RESPONSE = 24000
MAX_INPUT = 6000

# Tools the dashboard has no interest in at all.
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
    """Keep the tool input, minus the huge fields (prompts, part lists)."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        if isinstance(v, str):
            out[k] = v[:MAX_INPUT]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            # asset_pack's part list runs to tens of kilobytes: if it does not
            # fit, store clipped text — the paths are still recoverable from it
            text = json.dumps(v, ensure_ascii=False)
            out[k] = v if len(text) <= MAX_INPUT else text[:MAX_INPUT]
    return out


def session_id(data: dict) -> str:
    """Whose session this is. Claude Code puts session_id in every hook payload;
    the file is the fallback for events that somehow arrived without one."""
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
    # the name orders events with equal ts; pid separates concurrent hooks
    name = f"{time.time_ns():020d}-{os.getpid()}.json"
    tmp = EVENT_DIR / (name + ".part")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(EVENT_DIR / name)  # the server only ever sees a complete file


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
        shutil.rmtree(EVENT_DIR, ignore_errors=True)  # every session gets its own feed
        EVENTS.unlink(missing_ok=True)
        sid = str(data.get("session_id") or time.time_ns())
        SESSION_FILE.write_text(json.dumps({"id": sid, "ts": time.time()}),
                                encoding="utf-8")
        append({"event": "SessionStart"}, sid)
        start_server(open_browser=True)
        print(json.dumps({"suppressOutput": True}, ensure_ascii=False))
        return 0

    if mode == "stop":
        # turn is over: whatever is still marked "running" has already finished
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
        # the dashboard is not worth crashing a hook over
        sys.exit(0)
