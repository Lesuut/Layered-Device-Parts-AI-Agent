#!/usr/bin/env python3
"""Prompt guard: flow_generate must be given text straight from Prompts/*.txt.

The user edits these prompts by hand, and edits them often. An agent holding
them in context from last time will happily send a stale copy — there is nothing
to see, and the generation is already spent. So the check is a hook rather than
an instruction in a skill: the text is compared against the files on disk, and
on a mismatch the call goes to the user for confirmation.

Wired to PreToolUse with matcher mcp__flow-images__flow_generate.
Comparison runs on collapsed whitespace: a line break or extra indent is not a
mismatch, changed words are.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "Prompts"


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def known_prompts() -> dict[str, str]:
    out = {}
    if PROMPTS_DIR.is_dir():
        for f in sorted(PROMPTS_DIR.glob("*.txt")):
            try:
                out[f.name] = norm(f.read_text(encoding="utf-8"))
            except OSError:
                continue
    return out


def verdict(prompt: str) -> dict:
    files = known_prompts()
    if not files:
        return {}  # no prompts on disk — stay out of the way

    text = norm(prompt)
    for name, body in files.items():
        if text == body:
            return {}  # exact match with a file, all honest

    # nearest file — so the reason says what exactly it drifted from
    def overlap(body: str) -> float:
        a, b = set(text.split()), set(body.split())
        return len(a & b) / max(len(a | b), 1)

    near, score = max(((n, overlap(b)) for n, b in files.items()), key=lambda kv: kv[1])
    reason = (
        f"Prompt does not match any file in Prompts/ (nearest is \"{near}\", "
        f"{score * 100:.0f}% overlap). The user edits these prompts by hand: "
        f"read the right file with Read and send its text verbatim rather than "
        f"from memory. Allow only if the prompt was changed on purpose."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0

    prompt = (data.get("tool_input") or {}).get("prompt")
    if not prompt:
        return 0

    out = verdict(str(prompt))
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # the guard must never break the run
