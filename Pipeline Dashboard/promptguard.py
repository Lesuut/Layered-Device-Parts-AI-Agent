#!/usr/bin/env python3
"""Сторож промтов: flow_generate обязан получать текст из Promts/*.txt.

Промты пользователь правит руками, и правит часто. Агент, который держит их
в контексте с прошлого раза, легко отправит устаревшую копию — визуально
разницы не видно, а генерация уже потрачена. Поэтому проверку делает не
инструкция в скилле, а хук: текст сверяется с файлами на диске, и при
расхождении вызов уходит на подтверждение пользователю.

Подключается на PreToolUse с matcher mcp__flow-images__flow_generate.
Сравнение по «сжатым» пробелам: перенос строки или лишний отступ не считается
расхождением, изменение слов — считается.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "Promts"


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
        return {}  # промтов нет — не мешаем

    text = norm(prompt)
    for name, body in files.items():
        if text == body:
            return {}  # точное совпадение с файлом, всё честно

    # ближайший файл — чтобы в причине было видно, от чего именно отклонились
    def overlap(body: str) -> float:
        a, b = set(text.split()), set(body.split())
        return len(a & b) / max(len(a | b), 1)

    near, score = max(((n, overlap(b)) for n, b in files.items()), key=lambda kv: kv[1])
    reason = (
        f"Промт не совпадает с файлами в Promts/ (ближайший — «{near}», "
        f"совпадение {score * 100:.0f}%). Промты пользователь правит руками: "
        f"прочитай нужный файл через Read и отправь его текст дословно, "
        f"а не по памяти. Разрешай только если промт менялся осознанно."
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
        sys.exit(0)  # сторож не должен ломать работу
