#!/usr/bin/env python3
"""
Launcher: brings up Chrome with a debug port itself (a separate profile, the normal
Chrome does NOT have to be closed), waits for the port, lists the tabs and starts generation.

  python run.py                      # it will ask for a prompt
  python run.py --prompt "text"
  python run.py --setup-only         # only bring Chrome up and sign in
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
PORT = PORTS[0]  # chosen at startup: a port taken by another application is skipped
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
    """To stderr — so the module can be called from the MCP server (stdout = protocol)."""
    print(msg, file=sys.stderr, flush=True)


def probe(port: int, timeout: float = 2.0) -> dict | None:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_chrome(info: dict | None) -> bool:
    """Other applications answer on /json/version too (Lens Studio, for instance) —
    we tell the real Chrome apart by the Browser string and the presence of a ws endpoint."""
    if not info:
        return False
    return bool(info.get("webSocketDebuggerUrl")) and bool(
        re.match(r"(Headless)?Chrom(e|ium)/", str(info.get("Browser", "")))
    )


def cdp_version(timeout: float = 2.0) -> dict | None:
    """Find Chrome with debugging on one of the ports; PORT is switched to it."""
    global PORT
    for port in PORTS:
        info = probe(port, timeout)
        if is_chrome(info):
            PORT = port
            return info
    return None


def pick_free_port() -> int:
    """The port to launch on: the first one where nobody answers at all."""
    for port in PORTS:
        if probe(port, 1.0) is None:
            return port
    return PORTS[0]


def find_chrome() -> Path:
    for p in CHROME_CANDIDATES:
        if p and p.is_file():
            return p
    raise SystemExit(
        "chrome.exe was not found in the standard places.\n"
        "Write the path into CHROME_CANDIDATES at the top of run.py"
    )


def launch_chrome() -> None:
    global PORT
    chrome = find_chrome()
    PORT = pick_free_port()
    PROFILE.mkdir(parents=True, exist_ok=True)
    say(f"Chrome     : {chrome}")
    say(f"Profile    : {PROFILE}")
    say(f"CDP port   : {PORT}")
    say("The normal Chrome does not have to be closed — this is a separate process.")

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
    say("Waiting for Chrome to open the debug port...")
    deadline = time.time() + seconds
    while time.time() < deadline:
        info = probe(PORT)
        if is_chrome(info):
            say(f"OK: {info.get('Browser', '?')}  (port {PORT})")
            return info
        time.sleep(1)
    raise SystemExit(
        f"Chrome did not open port {PORT} within {seconds} s.\n"
        f"Check by hand: http://localhost:{PORT}/json/version\n"
        f"If no Chrome window appeared — delete the {PROFILE} folder and try again."
    )


def list_tabs() -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        say(f"Could not get the tab list: {exc}")
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
        say("Installing playwright...")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "playwright"])
        if rc != 0:
            raise SystemExit("Could not install playwright")


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
        help="A reference picture (can be repeated): pasted into Flow before generation",
    )
    args = ap.parse_args()

    say("=" * 60)
    say("  Google Flow — image generation")
    say("=" * 60)
    say(f"Python     : {sys.version.split()[0]}")
    ensure_playwright()
    say("playwright : OK")
    say("")

    info = cdp_version()
    if info:
        say(f"Chrome with debugging is already running: {info.get('Browser', '?')} (port {PORT})")
    else:
        say("Chrome with debugging is not running — starting a new one.")
        launch_chrome()
        wait_for_port()

    tabs = list_tabs()
    say("")
    say("Tabs in this Chrome:")
    for i, t in enumerate(tabs, 1):
        say(f"  {i:2}. {t.get('title', '')[:55]:55} | {t.get('url', '')[:70]}")
    if not tabs:
        say("  (empty)")

    has_flow = any("labs.google" in t.get("url", "") for t in tabs)
    if not has_flow:
        say("")
        say("There is no Flow tab. In the Chrome window that opened:")
        say("  1) sign in to your Google account")
        say("  2) open your Flow project")
        input("Done? Press Enter...")

    if args.setup_only:
        say("Chrome is ready. Next: python run.py --prompt \"text\"")
        return 0

    prompt = args.prompt
    if not prompt:
        say("")
        prompt = input("Prompt (Enter — a test one): ").strip()
    if not prompt:
        prompt = (
            "exploded view of a Nokia 3310 phone, all internal parts laid out flat, "
            "technical illustration, white background"
        )
        say(f"Test prompt: {prompt}")

    out = Path(args.out)
    rc = run_generation(prompt, args.count, out, args.ref)
    say("-" * 60)
    if rc == 0 and out.exists():
        say(f"Done. Folder: {out}")
        os.startfile(str(out))  # noqa: S606
    else:
        say(f"The script exited with code {rc}. See the log above.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
