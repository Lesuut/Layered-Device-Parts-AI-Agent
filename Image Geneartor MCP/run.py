#!/usr/bin/env python3
"""
Пусковик: сам поднимает Chrome с портом отладки (отдельный профиль, обычный
Chrome закрывать НЕ нужно), ждёт порт, показывает вкладки и запускает генерацию.

  python run.py                      # спросит промт
  python run.py --prompt "текст"
  python run.py --setup-only         # только поднять Chrome и залогиниться
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORTS = (9222, 9223, 9224)
PORT = PORTS[0]  # выбирается на старте: занятый чужим приложением порт пропускаем
PROFILE = Path(os.environ.get("LOCALAPPDATA", HERE)) / "ChromeDebugProfile"
FLOW_URL = "https://labs.google/fx/ru/tools/flow"

CHROME_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


for _stream in (sys.stderr, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(msg: str = "") -> None:
    """В stderr — чтобы модуль можно было звать из MCP-сервера (stdout = протокол)."""
    print(msg, file=sys.stderr, flush=True)


def probe(port: int, timeout: float = 2.0) -> dict | None:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_chrome(info: dict | None) -> bool:
    """На /json/version отвечают и другие приложения (например Lens Studio) —
    отличаем настоящий Chrome по строке Browser и наличию ws-эндпоинта."""
    if not info:
        return False
    return bool(info.get("webSocketDebuggerUrl")) and bool(
        re.match(r"(Headless)?Chrom(e|ium)/", str(info.get("Browser", "")))
    )


def cdp_version(timeout: float = 2.0) -> dict | None:
    """Найти Chrome с отладкой на одном из портов; PORT переставляется на него."""
    global PORT
    for port in PORTS:
        info = probe(port, timeout)
        if is_chrome(info):
            PORT = port
            return info
    return None


def pick_free_port() -> int:
    """Порт для запуска: первый, где вообще никто не отвечает."""
    for port in PORTS:
        if probe(port, 1.0) is None:
            return port
    return PORTS[0]


def find_chrome() -> Path:
    for p in CHROME_CANDIDATES:
        if p and p.is_file():
            return p
    raise SystemExit(
        "chrome.exe не найден в стандартных местах.\n"
        "Пропиши путь в CHROME_CANDIDATES вверху run.py"
    )


def launch_chrome() -> None:
    global PORT
    chrome = find_chrome()
    PORT = pick_free_port()
    PROFILE.mkdir(parents=True, exist_ok=True)
    say(f"Chrome     : {chrome}")
    say(f"Профиль    : {PROFILE}")
    say(f"Порт CDP   : {PORT}")
    say("Обычный Chrome закрывать не нужно — это отдельный процесс.")

    cmd = [
        str(chrome),
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        FLOW_URL,
    ]
    subprocess.Popen(cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def wait_for_port(seconds: int = 40) -> dict:
    say("Жду, пока Chrome поднимет порт отладки...")
    deadline = time.time() + seconds
    while time.time() < deadline:
        info = probe(PORT)
        if is_chrome(info):
            say(f"OK: {info.get('Browser', '?')}  (порт {PORT})")
            return info
        time.sleep(1)
    raise SystemExit(
        f"Chrome не открыл порт {PORT} за {seconds} с.\n"
        f"Проверь вручную: http://localhost:{PORT}/json/version\n"
        f"Если окно Chrome не появилось — удали папку {PROFILE} и повтори."
    )


def list_tabs() -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        say(f"Не смог получить список вкладок: {exc}")
        return []
    return [t for t in data if t.get("type") == "page"]


def run_generation(prompt: str, count: int, out: Path, refs: list[str] | None = None) -> int:
    cmd = [
        sys.executable,
        str(HERE / "flow_gen.py"),
        "--prompt", prompt,
        "--count", str(count),
        "--out", str(out),
        "--url", FLOW_URL,
        "--cdp", str(PORT),
    ]
    for ref in refs or []:
        cmd += ["--ref", ref]
    say("")
    say("-" * 60)
    return subprocess.call(cmd, cwd=str(HERE))


def ensure_playwright() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        say("Ставлю playwright...")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "playwright"])
        if rc != 0:
            raise SystemExit("Не удалось поставить playwright")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--out", default=str(HERE / "output"))
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument(
        "--ref",
        action="append",
        default=[],
        help="Картинка-референс (можно несколько раз): вставится в Flow до генерации",
    )
    args = ap.parse_args()

    say("=" * 60)
    say("  Google Flow — генерация картинок")
    say("=" * 60)
    say(f"Python     : {sys.version.split()[0]}")
    ensure_playwright()
    say("playwright : OK")
    say("")

    info = cdp_version()
    if info:
        say(f"Chrome с отладкой уже работает: {info.get('Browser', '?')} (порт {PORT})")
    else:
        say("Chrome с отладкой не запущен — запускаю новый.")
        launch_chrome()
        wait_for_port()

    tabs = list_tabs()
    say("")
    say("Вкладки в этом Chrome:")
    for i, t in enumerate(tabs, 1):
        say(f"  {i:2}. {t.get('title', '')[:55]:55} | {t.get('url', '')[:70]}")
    if not tabs:
        say("  (пусто)")

    has_flow = any("labs.google" in t.get("url", "") for t in tabs)
    if not has_flow:
        say("")
        say("Вкладки Flow нет. В открывшемся Chrome:")
        say("  1) войди в Google-аккаунт")
        say("  2) открой свой проект Flow")
        input("Готово? Нажми Enter...")

    if args.setup_only:
        say("Chrome готов. Дальше: python run.py --prompt \"текст\"")
        return 0

    prompt = args.prompt
    if not prompt:
        say("")
        prompt = input("Промт (Enter — тестовый): ").strip()
    if not prompt:
        prompt = (
            "exploded view of a Nokia 3310 phone, all internal parts laid out flat, "
            "technical illustration, white background"
        )
        say(f"Тестовый промт: {prompt}")

    out = Path(args.out)
    rc = run_generation(prompt, args.count, out, args.ref)
    say("-" * 60)
    if rc == 0 and out.exists():
        say(f"Готово. Папка: {out}")
        os.startfile(str(out))  # noqa: S606
    else:
        say(f"Скрипт завершился с кодом {rc}. Смотри лог выше.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
