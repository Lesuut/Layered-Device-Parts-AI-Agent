#!/usr/bin/env python3
"""
Automating Google Flow (labs.google/fx/tools/flow) in an ALREADY OPEN Chrome.

The script does not start its own browser and does not create a new profile — it
attaches to a running Chrome over CDP (the debug protocol), finds the Flow tab,
pastes the prompt, presses generate, waits for the pictures and downloads them into a folder.
All the Google cookies/login come from your normal profile.

ONE TIME: Chrome has to be started with a debug port.
  1. Close Chrome completely (the tray icon too).
  2. Run start_chrome_debug.bat  (it sits next to the script)
  3. Open your Flow project in that Chrome.

Usage:
  python flow_gen.py --prompt "prompt text"
  python flow_gen.py --prompt "..." --out "C:\\images" --count 4
  python flow_gen.py --prompt "..." --ref ref.png   # reference: paste + wait
  python flow_gen.py --list-tabs          # see what we attached to
  python flow_gen.py --prompts-file p.txt # a batch of prompts separated by a --- line

Installation:
  pip install playwright        # 'playwright install' is NOT needed: we start no browser of our own
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

# ---------------------------------------------------------------------------
# Element lookup settings (Flow changes its markup — fix it here)
# ---------------------------------------------------------------------------

FLOW_URL_MARK = "labs.google"          # what we recognise the right tab by
FLOW_PATH_MARK = "/tools/flow"

PROMPT_SELECTORS = [
    'textarea[placeholder*="Что вы хотите"]',
    'textarea[placeholder*="What do you want"]',
    'textarea[placeholder*="Опишите"]',
    'textarea[placeholder*="Describe"]',
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    "textarea",
]

# The generate button in Flow: no aria-label, inside is a material icon ligature
# "arrow_forward" and the caption "Создать". While the prompt is empty it is disabled.
SUBMIT_SELECTORS = [
    'button:has-text("arrow_forward")',
    'button:has(i:text-is("arrow_forward"))',
    'button[aria-label*="Создать"]',
    'button[aria-label*="Сгенерировать"]',
    'button[aria-label*="Generate"]',
    'button[aria-label*="Submit"]',
    'button[type="submit"]',
]

# buttons that look similar but do NOT start generation
SUBMIT_EXCLUDE_TEXT = ("add_2", "Агент", "Agent", "more_vert", "add\n")

IMG_URL_BLACKLIST = re.compile(
    r"(googleusercontent\.com/a/|/avatar|favicon|sprite|logo|icon)", re.I
)

# A reference appears in Flow as a thumbnail in the composer itself (next to the input).
# While the file is uploading, the generate button stays disabled.
REF_THUMB_MAX_SIDE = 220        # px: anything bigger is a generation result, not a chip
REF_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")

JS_COLLECT_IMAGES = """
(minSide) => {
  const out = [];
  for (const img of document.querySelectorAll('img')) {
    const url = img.currentSrc || img.src || '';
    if (!url) continue;
    const w = img.naturalWidth || img.width || 0;
    const h = img.naturalHeight || img.height || 0;
    if (w < minSide || h < minSide) continue;
    out.push({ url, w, h });
  }
  for (const el of document.querySelectorAll('[style*="background-image"]')) {
    const bg = getComputedStyle(el).backgroundImage || '';
    const m = bg.match(/url\\(["']?(.*?)["']?\\)/);
    if (!m) continue;
    const r = el.getBoundingClientRect();
    if (r.width < minSide || r.height < minSide) continue;
    out.push({ url: m[1], w: Math.round(r.width), h: Math.round(r.height) });
  }
  return out;
}
"""

JS_FETCH_AS_B64 = """
async (url) => {
  const resp = await fetch(url);
  const buf = await resp.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let bin = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return { b64: btoa(bin), type: resp.headers.get('content-type') || '' };
}
"""


# Synthetic file paste: we build a File out of base64, put it into a DataTransfer
# and fire a real ClipboardEvent('paste') — the same event as Ctrl+V,
# but without depending on the system clipboard.
JS_PASTE_FILE = """
([b64, mime, name, selector]) => {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const file = new File([bytes], name, { type: mime });

  const dt = new DataTransfer();
  dt.items.add(file);

  let target = selector ? document.querySelector(selector) : null;
  if (!target && document.activeElement && document.activeElement !== document.body) {
    target = document.activeElement;
  }
  target = target || document.querySelector('textarea') || document.body;

  const ev = new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true, composed: true,
  });
  // In some builds clipboardData from the constructor does not make it through — a fallback.
  if (!ev.clipboardData || ev.clipboardData.files.length === 0) {
    try { Object.defineProperty(ev, 'clipboardData', { value: dt }); } catch (e) {}
  }
  target.dispatchEvent(ev);
  return { tag: target.tagName, files: dt.files.length };
}
"""

# Fallback: drag&drop of the same file into the composer area.
JS_DROP_FILE = """
([b64, mime, name, selector]) => {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const file = new File([bytes], name, { type: mime });

  const dt = new DataTransfer();
  dt.items.add(file);

  let target = selector ? document.querySelector(selector) : null;
  target = target || document.querySelector('textarea') || document.body;
  const r = target.getBoundingClientRect();
  const opts = {
    dataTransfer: dt, bubbles: true, cancelable: true, composed: true,
    clientX: r.x + r.width / 2, clientY: r.y + r.height / 2,
  };
  for (const type of ['dragenter', 'dragover', 'drop']) {
    target.dispatchEvent(new DragEvent(type, opts));
  }
  return true;
}
"""

# Thumbnail chips: small pictures (references), not generation results.
JS_COLLECT_THUMBS = """
(maxSide) => {
  const out = [];
  for (const img of document.querySelectorAll('img')) {
    const url = img.currentSrc || img.src || '';
    if (!url) continue;
    const r = img.getBoundingClientRect();
    const w = r.width || img.width || 0;
    const h = r.height || img.height || 0;
    if (w <= 8 || h <= 8) continue;
    if (w > maxSide || h > maxSide) continue;
    out.push({ url, w: Math.round(w), h: Math.round(h),
               ready: !!(img.complete && img.naturalWidth > 0) });
  }
  return out;
}
"""

# Whether the page is loading/processing (spinners, progress bars).
JS_IS_BUSY = """
() => {
  const sel = '[role="progressbar"], [aria-busy="true"], progress, ' +
              '[class*="spinner" i], [class*="Spinner" ], [class*="loading" i]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width > 4 && r.height > 4) return true;
  }
  return false;
}
"""


def _init_stream(stream):
    """UTF-8 on output: the Windows console otherwise chokes on non-ASCII (cp1251)."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return stream


_init_stream(sys.stderr)
_init_stream(sys.stdout)


def log(msg: str) -> None:
    """The log goes to stderr: stdout is taken by the MCP protocol, nothing may be written there."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Attaching to an already running Chrome
# ---------------------------------------------------------------------------

DEFAULT_PORTS = (9222, 9223, 9224)

HOWTO = f"""
No Chrome with an open debug port was found.

What to do:
  1. Close Chrome completely (check the tray and Task Manager: chrome.exe).
  2. Run RUN_TEST.bat (or start_chrome_debug.bat) next to this script
     — it brings Chrome up with port {DEFAULT_PORTS[0]}.
  3. Open the Flow project in it and run the script again.

Manual check: open http://localhost:{DEFAULT_PORTS[0]}/json/version
"""


def probe_cdp(endpoint: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_chrome_cdp(info: dict) -> bool:
    """Filter out other applications on the same port (Lens Studio, for instance)
    that also answer on /json/version but are not Chrome."""
    browser = str(info.get("Browser", ""))
    return bool(info.get("webSocketDebuggerUrl")) and re.match(
        r"(Headless)?Chrom(e|ium)/", browser
    ) is not None


def resolve_endpoint(explicit: str | None) -> str:
    """Find a live CDP endpoint: an explicit one, or by trying the standard ports."""
    candidates = [explicit] if explicit else [f"http://localhost:{p}" for p in DEFAULT_PORTS]
    for ep in candidates:
        if not ep:
            continue
        if ep.isdigit():
            ep = f"http://localhost:{ep}"
        info = probe_cdp(ep)
        if not info:
            continue
        if not is_chrome_cdp(info) and not explicit:
            log(f"Skipping {ep}: this is not Chrome ({info.get('Browser', '?')})")
            continue
        log(f"Connecting: {ep}  ({info.get('Browser', '?')})")
        return ep
    raise SystemExit(HOWTO)


class AttachedChrome:
    """Attaching to a live Chrome. The browser is NEVER closed by the script."""

    def __init__(self, endpoint: str, action_timeout: int) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        if not self._browser.contexts:
            raise SystemExit("The browser has no contexts/tabs at all")
        self.contexts: list[BrowserContext] = list(self._browser.contexts)
        for ctx in self.contexts:
            ctx.set_default_timeout(action_timeout * 1000)

    def all_pages(self) -> list[Page]:
        return [p for ctx in self.contexts for p in ctx.pages]

    def find_flow_page(self, url: str | None) -> Page:
        pages = self.all_pages()
        if not pages:
            raise SystemExit("No open tabs")

        # 1) an exact match with the --url that was passed
        if url:
            base = url.split("?")[0].rstrip("/")
            for p in pages:
                if p.url.split("?")[0].rstrip("/") == base:
                    log(f"Found a tab with the requested URL: {p.url}")
                    return self._focus(p)

        # 2) any Flow tab
        for p in pages:
            if FLOW_URL_MARK in p.url and FLOW_PATH_MARK in p.url:
                log(f"Found a Flow tab: {p.url}")
                return self._focus(p)

        # 3) open a new tab in the existing window (if we know the URL)
        if url:
            log("No Flow tab found — opening a new one in the current Chrome")
            page = self.contexts[0].new_page()
            page.goto(url, wait_until="domcontentloaded")
            return self._focus(page)

        raise SystemExit(
            "No Flow tab found. Open a Flow project in Chrome "
            "or pass --url https://labs.google/fx/ru/tools/flow/project/..."
        )

    @staticmethod
    def _focus(page: Page) -> Page:
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page

    def detach(self) -> None:
        """Detach without touching the browser itself."""
        try:
            self._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Working with the Flow page
# ---------------------------------------------------------------------------


def find_prompt_box(page: Page, timeout: int):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        for sel in PROMPT_SELECTORS:
            loc = page.locator(sel).last
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception as exc:
                last_err = exc
        page.wait_for_timeout(500)
    raise RuntimeError(f"Could not find the prompt input. Last error: {last_err}")


def flatten_prompt(prompt: str) -> str:
    """Flatten the prompt into a single line.

    In the Flow composer Enter = submit, so the text must not be typed with line
    breaks: the request would go off on the first empty line. Paragraphs are joined
    with ' | ', ordinary breaks with a space; the meaning of the prompt is unchanged.
    """
    paragraphs = [
        " ".join(line.strip() for line in block.splitlines() if line.strip())
        for block in re.split(r"\n\s*\n", prompt.replace("\r\n", "\n").replace("\r", "\n"))
    ]
    return " | ".join(p for p in paragraphs if p)


def type_prompt(page: Page, prompt: str, timeout: int) -> None:
    flat = flatten_prompt(prompt)
    box = find_prompt_box(page, timeout)
    box.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    box.type(flat, delay=8)
    if flat != prompt:
        log("The prompt was flattened into one line (Enter in the composer = submit)")
    log(f"Prompt pasted ({len(flat)} characters)")


# ---------------------------------------------------------------------------
# References: pasting a picture into the composer before generation
# ---------------------------------------------------------------------------


def load_reference(src: str | Path) -> tuple[bytes, str, str]:
    """Reduce a picture source to (bytes, mime, file name).

    Accepts: a file path, a data: URI, an http(s) link or bare base64 — so the model
    can hand the picture over in whatever form suits it.
    """
    text = str(src).strip().strip('"').strip("'")

    if text.startswith("data:"):
        head, _, payload = text.partition(",")
        mime = head[5:].split(";")[0] or "image/png"
        data = base64.b64decode(payload)
        return data, mime, f"ref{ext_from(mime, '', '.png')}"

    if text.startswith(("http://", "https://")):
        with urllib.request.urlopen(text, timeout=60) as r:
            data = r.read()
            mime = r.headers.get("content-type", "") or "image/png"
        return data, mime.split(";")[0], f"ref{ext_from(mime, text, '.png')}"

    path = Path(text)
    if path.exists() and path.is_file():
        data = path.read_bytes()
        # The file signature is more reliable than mimetypes: on Windows it takes types
        # from the registry and may return image/png for .webp, for instance.
        mime = sniff_mime(data)
        if mime == "application/octet-stream":
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return data, mime, path.name

    # last option: bare base64 with no prefix
    if len(text) > 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", text or " "):
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileNotFoundError(f"Reference not recognised: {text[:60]}...") from exc
        return data, sniff_mime(data), "ref.png"

    raise FileNotFoundError(f"Reference file not found: {text}")


def sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "application/octet-stream"


def _temp_ref_file(data: bytes, name: str) -> Path:
    """Put the bytes into a temporary file — needed for the system clipboard and upload."""
    import tempfile

    suffix = Path(name).suffix.lower()
    if suffix not in REF_ALLOWED_EXT:
        suffix = ext_from(sniff_mime(data), "", ".png")
    fd = tempfile.NamedTemporaryFile(prefix="flowref_", suffix=suffix, delete=False)
    try:
        fd.write(data)
    finally:
        fd.close()
    return Path(fd.name)


PS_COPY_IMAGE = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bytes = [System.IO.File]::ReadAllBytes('{path}')
$ms = New-Object System.IO.MemoryStream(,$bytes)
$img = [System.Drawing.Image]::FromStream($ms)
$data = New-Object System.Windows.Forms.DataObject
$data.SetData('PNG', $false, $ms)
$data.SetImage($img)
[System.Windows.Forms.Clipboard]::SetDataObject($data, $true)
"""


def copy_image_to_clipboard(path: Path) -> None:
    """Put the picture into the Windows system clipboard (PNG + Bitmap)."""
    if sys.platform != "win32":
        raise RuntimeError("The system clipboard is only supported on Windows")
    script = PS_COPY_IMAGE.replace("{path}", str(path).replace("'", "''"))
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "powershell failed").strip().splitlines()[0])


def press_real_paste(page: Page) -> None:
    """A genuine Ctrl+V from the system clipboard.

    A plain keyboard.press('Control+V') over CDP does not trigger the paste command
    in the editor — Input.dispatchKeyEvent with commands: ['paste'] is needed.
    """
    cdp = page.context.new_cdp_session(page)
    try:
        base = {
            "modifiers": 2,  # Ctrl
            "windowsVirtualKeyCode": 86,
            "nativeVirtualKeyCode": 86,
            "key": "v",
            "code": "KeyV",
        }
        cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "commands": ["paste"], **base})
        cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
    finally:
        try:
            cdp.detach()
        except Exception:
            pass


def collect_thumbs(page: Page, max_side: int = REF_THUMB_MAX_SIDE) -> set[str]:
    try:
        items = page.evaluate(JS_COLLECT_THUMBS, max_side)
    except Exception:
        return set()
    return {it["url"] for it in items if not IMG_URL_BLACKLIST.search(it["url"])}


def page_busy(page: Page) -> bool:
    try:
        return bool(page.evaluate(JS_IS_BUSY))
    except Exception:
        return False


def wait_ref_uploaded(
    page: Page,
    baseline: set[str],
    timeout: int,
    stable_polls: int = 3,
    thumb_max: int = REF_THUMB_MAX_SIDE,
) -> str | None:
    """Wait for the reference to finish uploading to the site.

    Ready = a new thumbnail chip appeared, it is rendered (naturalWidth>0),
    there are no spinners and the set of chips is unchanged for several polls in a row.
    Returns the chip url or None if nothing appeared.
    """
    deadline = time.time() + timeout
    stable = 0
    prev: set[str] = set()
    last_new: str | None = None

    while time.time() < deadline:
        try:
            items = page.evaluate(JS_COLLECT_THUMBS, thumb_max)
        except Exception:
            items = []
        new = {
            it["url"]: it
            for it in items
            if it["url"] not in baseline and not IMG_URL_BLACKLIST.search(it["url"])
        }
        ready = all(it["ready"] for it in new.values()) and bool(new)

        if new and set(new) == prev and ready and not page_busy(page):
            stable += 1
            if stable >= stable_polls:
                last_new = sorted(new)[-1]
                log(f"Reference finished uploading ({len(new)} thumbnails in the composer)")
                return last_new
        else:
            stable = 0

        prev = set(new)
        page.wait_for_timeout(500)

    return None


def attach_reference(
    page: Page,
    src: str | Path,
    method: str = "auto",
    timeout: int = 90,
    action_timeout: int = 60,
    thumb_max: int = REF_THUMB_MAX_SIDE,
) -> dict:
    """Paste a reference picture into the Flow composer and wait for it to upload.

    Strategies in order (`method='auto'`):
      paste     — a synthetic ClipboardEvent with the file (fast, no OS clipboard);
      clipboard — the picture into the system clipboard + a genuine Ctrl+V over CDP;
      drop      — a synthetic drag&drop of the file into the composer;
      upload    — putting the file straight into input[type=file].
    """
    data, mime, name = load_reference(src)
    b64 = base64.b64encode(data).decode("ascii")
    log(f"Reference: {name} ({len(data) // 1024} KB, {mime})")

    box = find_prompt_box(page, action_timeout)
    try:
        box.click()
    except Exception:
        pass

    # A mark on the input field: the JS finds the paste/drop target by it.
    try:
        box.evaluate("el => el.setAttribute('data-flowref', '1')")
        target_sel = '[data-flowref="1"]'
    except Exception:
        target_sel = None

    order = ["paste", "clipboard", "drop", "upload"] if method == "auto" else [method]
    tmp: Path | None = None
    errors: list[str] = []

    try:
        for strategy in order:
            baseline = collect_thumbs(page, thumb_max)
            try:
                if strategy == "paste":
                    res = page.evaluate(JS_PASTE_FILE, [b64, mime, name, target_sel])
                    log(f"  paste -> {res}")

                elif strategy == "clipboard":
                    tmp = tmp or _temp_ref_file(data, name)
                    copy_image_to_clipboard(tmp)
                    try:
                        box.click()
                    except Exception:
                        pass
                    press_real_paste(page)
                    log("  Ctrl+V from the system clipboard sent")

                elif strategy == "drop":
                    page.evaluate(JS_DROP_FILE, [b64, mime, name, target_sel])
                    log("  drag&drop sent")

                elif strategy == "upload":
                    tmp = tmp or _temp_ref_file(data, name)
                    inputs = page.locator('input[type="file"]')
                    if not inputs.count():
                        raise RuntimeError("there is no input[type=file] on the page")
                    inputs.last.set_input_files(str(tmp))
                    log("  the file was put into input[type=file]")

                else:
                    raise ValueError(f"Unknown paste method: {strategy}")

            except Exception as exc:
                msg = str(exc).splitlines()[0]
                errors.append(f"{strategy}: {msg}")
                log(f"  method '{strategy}' did not work: {msg}")
                continue

            thumb = wait_ref_uploaded(page, baseline, timeout, thumb_max=thumb_max)
            if thumb:
                return {"ok": True, "method": strategy, "thumb": thumb, "name": name}

            errors.append(f"{strategy}: the thumbnail did not appear within {timeout} s")
            log(f"  method '{strategy}': the thumbnail never appeared")

        return {"ok": False, "method": None, "errors": errors, "name": name}

    finally:
        try:
            page.evaluate(
                "() => document.querySelectorAll('[data-flowref]')"
                ".forEach(e => e.removeAttribute('data-flowref'))"
            )
        except Exception:
            pass
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass


def attach_references(
    page: Page,
    refs: Iterable[str | Path],
    method: str = "auto",
    timeout: int = 90,
    action_timeout: int = 60,
    thumb_max: int = REF_THUMB_MAX_SIDE,
) -> list[dict]:
    results = []
    for i, ref in enumerate(refs, 1):
        log(f"=== Reference {i} ===")
        results.append(
            attach_reference(page, ref, method, timeout, action_timeout, thumb_max)
        )
    return results


def _submit_candidates(page: Page):
    """Every visible candidate button for 'generate', with their locators."""
    seen: set[str] = set()
    for sel in SUBMIT_SELECTORS:
        loc = page.locator(sel)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            item = loc.nth(i)
            try:
                if not item.is_visible():
                    continue
                text = (item.inner_text() or "").strip()
            except Exception:
                continue
            if any(bad in text for bad in SUBMIT_EXCLUDE_TEXT):
                continue
            key = f"{sel}#{i}"
            if key in seen:
                continue
            seen.add(key)
            yield sel, item, text


def find_submit(page: Page, wait_enabled: int = 0):
    """Find the generate button. wait_enabled — how many seconds to wait for it to become active."""
    deadline = time.time() + max(wait_enabled, 0)
    first_seen: tuple[str, object, str] | None = None

    while True:
        for sel, item, text in _submit_candidates(page):
            first_seen = first_seen or (sel, item, text)
            try:
                if item.is_enabled():
                    return sel, item, text
            except Exception:
                continue
        if time.time() >= deadline:
            break
        page.wait_for_timeout(500)

    return first_seen  # found, but never became active (or None)


def clear_prompt(page: Page, timeout: int) -> None:
    try:
        box = find_prompt_box(page, timeout)
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    except Exception:
        pass


def click_generate(page: Page, wait_enabled: int = 15) -> None:
    found = find_submit(page, wait_enabled=wait_enabled)
    if found:
        sel, item, text = found
        try:
            if item.is_enabled():
                item.click()
                log(f"Pressed the generate button [{text.replace(chr(10), ' | ')}] ({sel})")
                return
            log(f"The button [{text.replace(chr(10), ' | ')}] stayed inactive — pressing Enter")
        except Exception as exc:
            log(f"The click did not go through ({exc}) — pressing Enter")
    else:
        log("Could not find the button — pressing Enter")
    page.keyboard.press("Enter")


def collect_images(page: Page, min_side: int) -> dict[str, tuple[int, int]]:
    try:
        items = page.evaluate(JS_COLLECT_IMAGES, min_side)
    except Exception:
        return {}
    result: dict[str, tuple[int, int]] = {}
    for it in items:
        url = it["url"]
        if not url or IMG_URL_BLACKLIST.search(url):
            continue
        result[url] = (it["w"], it["h"])
    return result


def wait_for_new_images(
    page: Page,
    baseline: Iterable[str],
    count: int,
    timeout: int,
    min_side: int,
    stable_polls: int = 3,
) -> list[str]:
    """Wait for count new pictures and for the set to settle (so we do not catch a preview)."""
    baseline = set(baseline)
    deadline = time.time() + timeout
    stable = 0
    prev_new: list[str] = []

    while time.time() < deadline:
        new = [u for u in collect_images(page, min_side) if u not in baseline]

        if new == prev_new and len(new) >= count:
            stable += 1
            if stable >= stable_polls:
                log(f"Done: {len(new)} new pictures")
                return new[:count] if count > 0 else new
        else:
            stable = 0

        if new != prev_new:
            log(f"New pictures: {len(new)}/{count}")
        prev_new = new
        page.wait_for_timeout(2000)

    if prev_new:
        log(f"Timeout, but {len(prev_new)} pictures turned up — downloading them")
        return prev_new
    raise TimeoutError("Never got the generated pictures")


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


# --- downloading through the card menu: more_vert -> Download -> 1K/2K/4K -----

# Mark the card holding the picture with the given src using a data-flowdl attribute.
# That way it can be taken with a plain CSS selector, with no URL escaping.
JS_MARK_CARD = """
(url) => {
  for (const el of document.querySelectorAll('[data-flowdl]')) el.removeAttribute('data-flowdl');
  let img = null;
  for (const cand of document.querySelectorAll('img')) {
    if ((cand.currentSrc || cand.src) === url) { img = cand; break; }
  }
  if (!img) return null;

  // The favorite/redo/more_vert buttons live in the card overlay, which
  // appears on hover. We look for an ancestor with role="button", otherwise the largest one.
  let node = img, best = null;
  for (let i = 0; i < 10 && node; i++) {
    node = node.parentElement;
    if (!node) break;
    const r = node.getBoundingClientRect();
    if (r.width < 200 || r.height < 150) continue;
    if (!best) best = node;
    if (node.getAttribute('role') === 'button') { best = node; break; }
    if (r.width > 900) break;  // we went into the whole grid
  }
  if (!best) return null;

  best.setAttribute('data-flowdl', '1');
  best.scrollIntoView({ block: 'center', behavior: 'instant' });
  const r = best.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height, role: best.getAttribute('role') };
}
"""

CARD_SEL = '[data-flowdl="1"]'
MORE_BTN_SEL = 'button:has-text("more_vert")'
DOWNLOAD_ITEM_SEL = (
    '[role="menuitem"]:has-text("Скачать"), [role="menuitem"]:has-text("Download")'
)
QUALITY_LABELS = {"1k": "1K", "2k": "2K", "4k": "4K"}


# The "Upscaling finished" toasts hang in the top right corner and cover the
# cards of the third column — hover does not reach them.
JS_DISMISS_TOASTS = """
() => {
  let n = 0;
  for (const ol of document.querySelectorAll('section > ol, ol[role="region"]')) {
    if (!ol.querySelector('li')) continue;
    ol.style.pointerEvents = 'none';
    for (const li of ol.querySelectorAll('li')) {
      li.style.pointerEvents = 'none';
      n++;
    }
  }
  return n;
}
"""


JS_HIT_TEST = """
([x, y]) => {
  const card = document.querySelector('[data-flowdl="1"]');
  if (!card) return null;
  const top = document.elementFromPoint(x, y);
  if (!top) return { inside: false, what: 'nothing (outside the window)' };
  return {
    inside: card.contains(top) || top === card,
    what: (top.innerText || top.tagName).trim().replace(/\\n/g, ' '),
  };
}
"""


def dismiss_toasts(page: Page) -> None:
    """Get the popup notifications out of the cursor's path."""
    # the regular "Close" button first, if there is one
    try:
        close_btns = page.locator('li button:has-text("Закрыть"), li button:has-text("Close")')
        for i in range(min(close_btns.count(), 5)):
            btn = close_btns.nth(i)
            if btn.is_visible():
                btn.click(timeout=1500)
    except Exception:
        pass
    # then remove mouse interception from the rest
    try:
        page.evaluate(JS_DISMISS_TOASTS)
    except Exception:
        pass


def close_menus(page: Page, timeout: int = 3000) -> None:
    """Close the open menus and wait for them to disappear."""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        try:
            if page.locator('[role="menu"]').count() == 0:
                return
            page.keyboard.press("Escape")
        except Exception:
            return
        page.wait_for_timeout(200)


def find_more_button_in(page: Page, rect: dict, margin: int = 12):
    """The more_vert button that falls inside the card rectangle.

    There is a more_vert in the page header too, so we filter by geometry.
    """
    loc = page.locator(MORE_BTN_SEL)
    for i in range(loc.count()):
        item = loc.nth(i)
        try:
            if not item.is_visible():
                continue
            box = item.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        inside_x = rect["x"] - margin <= cx <= rect["x"] + rect["w"] + margin
        inside_y = rect["y"] - margin <= cy <= rect["y"] + rect["h"] + margin
        if inside_x and inside_y:
            return item
    return None


def download_via_ui(
    page: Page,
    url: str,
    out_dir: Path,
    prefix: str,
    index: int,
    quality: str,
    timeout: int,
) -> Path | None:
    """Download a picture from the Flow menu at the required resolution. None — if it did not work."""
    label = QUALITY_LABELS.get(quality, "2K")

    rect = page.evaluate(JS_MARK_CARD, url)
    if not rect:
        log(f"  [{index}] no card found for the picture")
        return None

    more = None
    for attempt in range(1, 4):
        close_menus(page)
        dismiss_toasts(page)
        page.wait_for_timeout(300)  # finish scrolling / close the previous menu

        rect = page.evaluate(
            "() => { const e = document.querySelector('[data-flowdl=\"1\"]');"
            "  if (!e) return null; const r = e.getBoundingClientRect();"
            "  return {x: r.x, y: r.y, w: r.width, h: r.height}; }"
        )
        if not rect:
            return None

        cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2

        # what actually lies under the hover point
        blocker = page.evaluate(JS_HIT_TEST, [cx, cy])
        if blocker and not blocker["inside"]:
            log(f"  [{index}] the card is covered by: {blocker['what'][:50]} — moving it aside")
            page.mouse.wheel(0, 140 if attempt == 1 else -140)
            page.wait_for_timeout(500)
            continue

        # move the cursor away first, otherwise mouseenter does not fire again
        page.mouse.move(5, 5)
        page.wait_for_timeout(150)
        page.mouse.move(cx, cy, steps=8)
        page.wait_for_timeout(250)
        page.mouse.move(cx + 4, cy + 4)  # a wiggle finishes off the hover state
        page.wait_for_timeout(600)

        more = find_more_button_in(page, rect)
        if more is not None:
            break
        log(f"  [{index}] the card overlay did not appear (attempt {attempt}/3)")

    if more is None:
        log(f"  [{index}] the 'More' button on the card did not appear")
        return None
    more.click()
    page.wait_for_timeout(600)

    dl_item = page.locator(DOWNLOAD_ITEM_SEL).first
    dl_item.wait_for(state="visible", timeout=10_000)
    dl_item.hover()
    page.wait_for_timeout(700)

    # submenu item: "2K | Увеличенное разрешение"
    quality_item = page.locator(f'[role="menuitem"]:has-text("{label}")').last
    quality_item.wait_for(state="visible", timeout=10_000)

    log(f"  [{index}] pressing '{label}' (upscaling can take up to {timeout} s)")
    try:
        with page.expect_download(timeout=timeout * 1000) as dl_info:
            quality_item.click()
        download = dl_info.value
        suffix = Path(download.suggested_filename).suffix or ".png"
        path = out_dir / f"{prefix}_{index:02d}_{label.lower()}{suffix}"
        download.save_as(str(path))
        return path
    except Exception as exc:
        log(f"  [{index}] downloading through the menu failed: {str(exc).splitlines()[0]}")
        return None
    finally:
        try:
            page.keyboard.press("Escape")
            page.evaluate(
                "() => document.querySelectorAll('[data-flowdl]')"
                ".forEach(e => e.removeAttribute('data-flowdl'))"
            )
        except Exception:
            pass


def ext_from(content_type: str, url: str, default: str = ".png") -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ".jpg" if ext == ".jpe" else ext
    m = re.search(r"\.(png|jpe?g|webp|gif)(?:[?#]|$)", url, re.I)
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg")
    return default


def fetch_bytes(page: Page, url: str) -> tuple[bytes, str]:
    """blob:/data: — read inside the page itself; http(s) — through its context cookies."""
    if url.startswith(("blob:", "data:")):
        res = page.evaluate(JS_FETCH_AS_B64, url)
        return base64.b64decode(res["b64"]), res.get("type", "")

    resp = page.context.request.get(url, timeout=120_000)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}")
    return resp.body(), resp.headers.get("content-type", "")


def upgrade_url(url: str) -> str:
    """googleusercontent: strip the downsize suffix (=w512-h512 -> =s0)."""
    if "googleusercontent.com" in url:
        return re.sub(r"=[-\w]+$", "=s0", url)
    return url


def download_all(
    page: Page,
    urls: list[str],
    out_dir: Path,
    prefix: str,
    quality: str = "2k",
    ui_timeout: int = 180,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    seen_hashes: set[str] = set()

    for i, url in enumerate(urls, 1):
        # the main path: card menu -> Download -> 2K (with upscaling)
        if quality in QUALITY_LABELS:
            try:
                path = download_via_ui(page, url, out_dir, prefix, i, quality, ui_timeout)
            except Exception as exc:
                log(f"  [{i}] UI download error: {str(exc).splitlines()[0]}")
                path = None
            if path:
                seen_hashes.add(hashlib.sha1(path.read_bytes()).hexdigest())
                saved.append(path)
                log(f"  [{i}] saved: {path.name} ({path.stat().st_size // 1024} KB)")
                continue
            log(f"  [{i}] falling back to a direct download from src")

        for candidate in dict.fromkeys([upgrade_url(url), url]):
            try:
                data, ctype = fetch_bytes(page, candidate)
            except Exception as exc:
                log(f"  [{i}] did not download ({exc}) — trying another URL variant")
                continue

            digest = hashlib.sha1(data).hexdigest()
            if digest in seen_hashes:
                log(f"  [{i}] duplicate, skipping")
                break
            seen_hashes.add(digest)

            path = out_dir / f"{prefix}_{i:02d}{ext_from(ctype, candidate)}"
            path.write_bytes(data)
            saved.append(path)
            log(f"  [{i}] saved: {path.name} ({len(data) // 1024} KB)")
            break
        else:
            log(f"  [{i}] SKIPPED: {url[:120]}")

    return saved


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def read_prompts(args: argparse.Namespace) -> list[str]:
    if args.prompts_file:
        text = Path(args.prompts_file).read_text(encoding="utf-8")
        return [p.strip() for p in text.split("\n---\n") if p.strip()]
    if args.prompt:
        return [args.prompt]
    raise SystemExit("--prompt or --prompts-file is required")


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "_", text, flags=re.U).strip("_")
    return (s[:limit] or "gen").lower()


# ---------------------------------------------------------------------------
# The high-level API (the MCP server calls the same one)
# ---------------------------------------------------------------------------


def image_size(path: Path) -> tuple[int, int] | None:
    """JPEG/PNG dimensions with no external dependencies."""
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":  # JPEG: look for SOFn
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                w = int.from_bytes(data[i + 7 : i + 9], "big")
                return w, h
            seg = int.from_bytes(data[i + 2 : i + 4], "big")
            if seg <= 0:
                break
            i += 2 + seg
    return None


def generate_images(
    prompt: str,
    count: int = 4,
    out_dir: str | Path | None = None,
    quality: str = "2k",
    url: str | None = None,
    cdp: str | None = None,
    gen_timeout: int = 600,
    ui_timeout: int = 180,
    action_timeout: int = 60,
    min_side: int = 256,
    references: Iterable[str | Path] | None = None,
    ref_method: str = "auto",
    ref_timeout: int = 90,
    ref_required: bool = True,
    ref_thumb_max: int = REF_THUMB_MAX_SIDE,
) -> dict:
    """The full cycle: prompt -> (references) -> generation -> waiting -> downloading.

    Blocks until all `count` pictures have been saved.
    If `references` are passed, the pictures get pasted into the composer (like Ctrl+V),
    the script waits for them to finish uploading to the site and only then presses "generate".
    Returns a dict with the paths and metadata — the MCP server uses it.
    """
    started = time.time()
    chrome = AttachedChrome(resolve_endpoint(cdp), action_timeout)
    try:
        page = chrome.find_flow_page(url)

        if "accounts.google.com" in page.url:
            return {
                "ok": False,
                "error": "not_logged_in",
                "message": "Chrome is on the Google sign-in page. A manual sign-in is needed.",
                "images": [],
            }

        try:
            type_prompt(page, prompt, action_timeout)
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": "no_prompt_box",
                "message": (
                    f"{exc} It looks like the Flow landing page is open, not a project — "
                    f"open the project in this tab ({page.url})."
                ),
                "images": [],
            }

        refs = list(references or [])
        ref_results: list[dict] = []
        if refs:
            try:
                ref_results = attach_references(
                    page, refs, ref_method, ref_timeout, action_timeout, ref_thumb_max
                )
            except FileNotFoundError as exc:
                return {
                    "ok": False,
                    "error": "reference_not_found",
                    "message": str(exc),
                    "images": [],
                }
            failed = [r for r in ref_results if not r["ok"]]
            if failed and ref_required:
                clear_prompt(page, action_timeout)
                return {
                    "ok": False,
                    "error": "reference_upload_failed",
                    "message": (
                        f"Could not paste {len(failed)} of {len(refs)} references "
                        "— generation was not started. "
                        + "; ".join("/".join(f.get("errors", [])) for f in failed)[:400]
                    ),
                    "references": ref_results,
                    "images": [],
                }

        # The baseline set is taken AFTER pasting the references: otherwise a reference
        # thumbnail could end up among the "new pictures".
        baseline = set(collect_images(page, min_side))
        click_generate(page, wait_enabled=30 if refs else 15)

        try:
            urls = wait_for_new_images(page, baseline, count, gen_timeout, min_side)
        except (TimeoutError, PWTimeout) as exc:
            return {
                "ok": False,
                "error": "generation_timeout",
                "message": str(exc),
                "images": [],
            }

        root = Path(out_dir).resolve() if out_dir else (Path.cwd() / "output").resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = root / f"{stamp}_{slugify(prompt)}"
        saved = download_all(page, urls, folder, "img", quality=quality, ui_timeout=ui_timeout)

        images = []
        for p in saved:
            size = image_size(p)
            images.append(
                {
                    "path": str(p),
                    "filename": p.name,
                    "bytes": p.stat().st_size,
                    "width": size[0] if size else None,
                    "height": size[1] if size else None,
                    "upscaled_2k": "_2k" in p.stem,
                }
            )

        return {
            "ok": len(saved) == count,
            "requested": count,
            "generated": len(urls),
            "downloaded": len(saved),
            "folder": str(folder),
            "quality": quality,
            "prompt": prompt,
            "elapsed_sec": round(time.time() - started, 1),
            "references": [
                {"name": r.get("name"), "ok": r["ok"], "method": r.get("method")}
                for r in ref_results
            ],
            "images": images,
            "message": (
                f"Saved {len(saved)} of {count} pictures into {folder}"
                if len(saved) == count
                else f"WARNING: only {len(saved)} of {count} were saved. Folder: {folder}"
            ),
        }
    finally:
        chrome.detach()


def paste_references(
    references: Iterable[str | Path],
    url: str | None = None,
    cdp: str | None = None,
    ref_method: str = "auto",
    ref_timeout: int = 90,
    action_timeout: int = 60,
    ref_thumb_max: int = REF_THUMB_MAX_SIDE,
) -> dict:
    """Only paste the references into the Flow composer, without generating.

    Useful for checking that pasting works without spending quota.
    """
    chrome = AttachedChrome(resolve_endpoint(cdp), action_timeout)
    try:
        page = chrome.find_flow_page(url)
        if "accounts.google.com" in page.url:
            return {"ok": False, "error": "not_logged_in", "references": []}
        try:
            results = attach_references(
                page, references, ref_method, ref_timeout, action_timeout, ref_thumb_max
            )
        except FileNotFoundError as exc:
            return {"ok": False, "error": "reference_not_found", "message": str(exc)}
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": "no_prompt_box",
                "message": (
                    f"{exc} It looks like the Flow landing page is open, not a project — "
                    f"open the project in this tab ({page.url})."
                ),
            }
        ok = all(r["ok"] for r in results) and bool(results)
        return {
            "ok": ok,
            "references": results,
            "message": (
                "The references are in place, the composer is ready to generate"
                if ok
                else "Some references were not pasted — see errors"
            ),
        }
    finally:
        chrome.detach()


def check_environment(cdp: str | None = None) -> dict:
    """Is Chrome ready: debug port, Flow tab, login."""
    try:
        endpoint = resolve_endpoint(cdp)
    except SystemExit as exc:
        return {"ready": False, "reason": "chrome_not_running", "message": str(exc)}

    chrome = AttachedChrome(endpoint, 30)
    try:
        tabs = [{"title": p.title(), "url": p.url} for p in chrome.all_pages()]
        flow = [t for t in tabs if FLOW_URL_MARK in t["url"] and FLOW_PATH_MARK in t["url"]]
        login = [t for t in tabs if "accounts.google.com" in t["url"]]
        return {
            "ready": bool(flow) and not login,
            "endpoint": endpoint,
            "flow_tabs": flow,
            "all_tabs": tabs,
            "reason": (
                "ok" if flow else ("login_required" if login else "no_flow_tab")
            ),
            "message": (
                f"Ready. Flow project: {flow[0]['url']}"
                if flow
                else "Open a Flow project in the debug Chrome (or sign in)."
            ),
        }
    finally:
        chrome.detach()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generating pictures in Google Flow through an already open Chrome"
    )
    p.add_argument("--prompt", help="The prompt text")
    p.add_argument("--prompts-file", help="A file of prompts, separated by a '---' line")
    p.add_argument("--url", help="URL of the Flow project (if the tab is not open yet)")
    p.add_argument("--out", default="output", help="Folder for the pictures")
    p.add_argument("--count", type=int, default=4, help="How many pictures to wait for (0 = all new ones)")
    p.add_argument("--cdp", help=f"CDP endpoint or port (by default it tries {DEFAULT_PORTS})")
    p.add_argument("--list-tabs", action="store_true", help="Show the tabs and exit")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the selectors: paste the prompt, find the button, do NOT generate",
    )
    p.add_argument("--gen-timeout", type=int, default=600, help="Waiting for generation, sec")
    p.add_argument("--action-timeout", type=int, default=60, help="Action timeout, sec")
    p.add_argument("--min-side", type=int, default=256, help="Min. picture side, px")
    p.add_argument(
        "--quality",
        choices=["2k", "1k", "4k", "src"],
        default="2k",
        help="Resolution: 2k (upscaled, the default), 1k, 4k (needs a paid plan), "
        "src — a fast direct grab from <img> with no menu",
    )
    p.add_argument(
        "--ui-timeout", type=int, default=180, help="Waiting for upscale+download, sec"
    )
    p.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="PATH",
        help="A reference picture: a file path, a data: URI or a link. "
        "Can be given several times. Pasted into the composer before generation",
    )
    p.add_argument(
        "--ref-method",
        choices=["auto", "paste", "clipboard", "drop", "upload"],
        default="auto",
        help="How to paste the reference: auto — try each in turn, clipboard — a genuine Ctrl+V "
        "through the Windows system clipboard",
    )
    p.add_argument(
        "--ref-timeout", type=int, default=90, help="Waiting for the reference to upload, sec"
    )
    p.add_argument(
        "--ref-thumb-max",
        type=int,
        default=REF_THUMB_MAX_SIDE,
        help=f"Max. side of the reference thumbnail, px ({REF_THUMB_MAX_SIDE}). "
        "Raise it if Flow draws a bigger chip and the upload 'is not detected'",
    )
    p.add_argument(
        "--ref-optional",
        action="store_true",
        help="Generate even if the reference was not pasted (stop by default)",
    )
    p.add_argument("--settle", type=int, default=3, help="Pause before the start, sec")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    endpoint = resolve_endpoint(args.cdp)
    chrome = AttachedChrome(endpoint, args.action_timeout)

    try:
        if args.list_tabs:
            for i, pg in enumerate(chrome.all_pages(), 1):
                print(f"{i:2}. {pg.title()[:60]:60} | {pg.url}")
            return 0

        prompts = read_prompts(args)
        out_root = Path(args.out).resolve()

        page = chrome.find_flow_page(args.url)
        page.wait_for_timeout(args.settle * 1000)

        if "accounts.google.com" in page.url:
            input("A Google sign-in is needed. Sign in in the browser and press Enter here...")

        total = 0
        for idx, prompt in enumerate(prompts, 1):
            log(f"=== Prompt {idx}/{len(prompts)} ===")

            type_prompt(page, prompt, args.action_timeout)

            ref_failed = False
            if args.ref:
                results = attach_references(
                    page,
                    args.ref,
                    args.ref_method,
                    args.ref_timeout,
                    args.action_timeout,
                    args.ref_thumb_max,
                )
                ref_failed = any(not r["ok"] for r in results)
                if ref_failed and not args.ref_optional and not args.dry_run:
                    log("ERROR: the reference was not pasted — not starting generation "
                        "(--ref-optional to generate anyway)")
                    clear_prompt(page, args.action_timeout)
                    continue

            # the baseline set — after the references, otherwise a thumbnail lands among the "new" ones
            baseline = set(collect_images(page, args.min_side))
            log(f"Pictures on the page before generation: {len(baseline)}")

            if args.dry_run:
                found = find_submit(page, wait_enabled=10)
                if found:
                    sel, item, text = found
                    state = "active" if item.is_enabled() else "INACTIVE"
                    log(f"DRY-RUN: button [{text.replace(chr(10), ' | ')}] {state}, selector {sel}")
                else:
                    log("DRY-RUN: the button was NOT found (Enter will be used)")
                clear_prompt(page, args.action_timeout)
                log("DRY-RUN: the field was cleared, generation was not started")
                continue

            click_generate(page, wait_enabled=30 if args.ref else 15)

            try:
                urls = wait_for_new_images(
                    page, baseline, args.count, args.gen_timeout, args.min_side
                )
            except (TimeoutError, PWTimeout) as exc:
                log(f"ERROR: {exc}")
                continue

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            folder = out_root / f"{stamp}_{slugify(prompt)}"
            saved = download_all(
                page, urls, folder, "img", quality=args.quality, ui_timeout=args.ui_timeout
            )
            total += len(saved)
            log(f"Prompt {idx}: saved {len(saved)} -> {folder}")

        log(f"TOTAL: {total} pictures in {out_root}")
        return 0
    finally:
        chrome.detach()  # the browser stays open


if __name__ == "__main__":
    sys.exit(main())
