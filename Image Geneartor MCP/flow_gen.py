#!/usr/bin/env python3
"""
Автоматизация Google Flow (labs.google/fx/tools/flow) в УЖЕ ОТКРЫТОМ Chrome.

Скрипт не запускает свой браузер и не создаёт новый профиль — он цепляется
к работающему Chrome через CDP (протокол отладки), находит вкладку с Flow,
вставляет промт, жмёт генерацию, ждёт картинки и качает их в папку.
Все куки/логин Google берутся из твоего обычного профиля.

ОДИН РАЗ: Chrome должен быть запущен с портом отладки.
  1. Закрыть Chrome полностью (иконка в трее тоже).
  2. Запустить start_chrome_debug.bat  (лежит рядом со скриптом)
  3. Открыть свой проект Flow в этом Chrome.

Запуск:
  python flow_gen.py --prompt "текст промта"
  python flow_gen.py --prompt "..." --out "C:\\images" --count 4
  python flow_gen.py --prompt "..." --ref ref.png   # референс: вставка + ожидание
  python flow_gen.py --list-tabs          # посмотреть, к чему подключились
  python flow_gen.py --prompts-file p.txt # пачка промтов через строку ---

Установка:
  pip install playwright        # 'playwright install' НЕ нужен: свой браузер не поднимаем
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
# Настройки поиска элементов (Flow меняет вёрстку — правится тут)
# ---------------------------------------------------------------------------

FLOW_URL_MARK = "labs.google"          # по чему узнаём нужную вкладку
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

# Кнопка генерации во Flow: без aria-label, внутри — material-иконка-лигатура
# "arrow_forward" и подпись "Создать". Пока промт пуст, она disabled.
SUBMIT_SELECTORS = [
    'button:has-text("arrow_forward")',
    'button:has(i:text-is("arrow_forward"))',
    'button[aria-label*="Создать"]',
    'button[aria-label*="Сгенерировать"]',
    'button[aria-label*="Generate"]',
    'button[aria-label*="Submit"]',
    'button[type="submit"]',
]

# кнопки, которые похожи, но НЕ запускают генерацию
SUBMIT_EXCLUDE_TEXT = ("add_2", "Агент", "Agent", "more_vert", "add\n")

IMG_URL_BLACKLIST = re.compile(
    r"(googleusercontent\.com/a/|/avatar|favicon|sprite|logo|icon)", re.I
)

# Референс во Flow появляется миниатюрой в самом композере (рядом с полем ввода).
# Пока файл заливается, кнопка генерации остаётся disabled.
REF_THUMB_MAX_SIDE = 220        # px: больше — это уже результат генерации, не чип
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


# Синтетическая вставка файла: собираем File из base64, кладём в DataTransfer
# и шлём настоящий ClipboardEvent('paste') — то же событие, что и Ctrl+V,
# но без зависимости от системного буфера обмена.
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
  // В части сборок clipboardData из конструктора не долетает — подстраховка.
  if (!ev.clipboardData || ev.clipboardData.files.length === 0) {
    try { Object.defineProperty(ev, 'clipboardData', { value: dt }); } catch (e) {}
  }
  target.dispatchEvent(ev);
  return { tag: target.tagName, files: dt.files.length };
}
"""

# Резерв: drag&drop того же файла в зону композера.
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

# Миниатюры-чипы: маленькие картинки (референсы), а не результаты генерации.
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

# Идёт ли на странице загрузка/обработка (спиннеры, прогресс-бары).
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
    """UTF-8 на выводе: в консоли Windows иначе падает на кириллице (cp1251)."""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return stream


_init_stream(sys.stderr)
_init_stream(sys.stdout)


def log(msg: str) -> None:
    """Лог идёт в stderr: stdout занят протоколом MCP, туда писать нельзя."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Подключение к уже запущенному Chrome
# ---------------------------------------------------------------------------

DEFAULT_PORTS = (9222, 9223, 9224)

HOWTO = f"""
Не нашёл Chrome с открытым портом отладки.

Что сделать:
  1. Полностью закрыть Chrome (проверь трей и Диспетчер задач: chrome.exe).
  2. Запустить RUN_TEST.bat (или start_chrome_debug.bat) рядом с этим скриптом
     — поднимет Chrome с портом {DEFAULT_PORTS[0]}.
  3. Открыть в нём проект Flow и повторить запуск скрипта.

Проверка вручную: открой http://localhost:{DEFAULT_PORTS[0]}/json/version
"""


def probe_cdp(endpoint: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_chrome_cdp(info: dict) -> bool:
    """Отфильтровать чужие приложения на том же порту (напр. Lens Studio),
    которые тоже отвечают на /json/version, но не являются Chrome."""
    browser = str(info.get("Browser", ""))
    return bool(info.get("webSocketDebuggerUrl")) and re.match(
        r"(Headless)?Chrom(e|ium)/", browser
    ) is not None


def resolve_endpoint(explicit: str | None) -> str:
    """Найти живой CDP-эндпоинт: явный, либо перебор стандартных портов."""
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
            log(f"Пропускаю {ep}: это не Chrome ({info.get('Browser', '?')})")
            continue
        log(f"Подключение: {ep}  ({info.get('Browser', '?')})")
        return ep
    raise SystemExit(HOWTO)


class AttachedChrome:
    """Подключение к живому Chrome. Браузер НИКОГДА не закрывается скриптом."""

    def __init__(self, endpoint: str, action_timeout: int) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(endpoint)
        if not self._browser.contexts:
            raise SystemExit("В браузере нет ни одного контекста/вкладки")
        self.contexts: list[BrowserContext] = list(self._browser.contexts)
        for ctx in self.contexts:
            ctx.set_default_timeout(action_timeout * 1000)

    def all_pages(self) -> list[Page]:
        return [p for ctx in self.contexts for p in ctx.pages]

    def find_flow_page(self, url: str | None) -> Page:
        pages = self.all_pages()
        if not pages:
            raise SystemExit("Нет открытых вкладок")

        # 1) точное совпадение с переданным --url
        if url:
            base = url.split("?")[0].rstrip("/")
            for p in pages:
                if p.url.split("?")[0].rstrip("/") == base:
                    log(f"Нашёл вкладку с нужным URL: {p.url}")
                    return self._focus(p)

        # 2) любая вкладка Flow
        for p in pages:
            if FLOW_URL_MARK in p.url and FLOW_PATH_MARK in p.url:
                log(f"Нашёл вкладку Flow: {p.url}")
                return self._focus(p)

        # 3) открыть новую вкладку в существующем окне (если знаем URL)
        if url:
            log("Вкладка Flow не найдена — открываю новую в текущем Chrome")
            page = self.contexts[0].new_page()
            page.goto(url, wait_until="domcontentloaded")
            return self._focus(page)

        raise SystemExit(
            "Вкладка Flow не найдена. Открой проект Flow в Chrome "
            "или передай --url https://labs.google/fx/ru/tools/flow/project/..."
        )

    @staticmethod
    def _focus(page: Page) -> Page:
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page

    def detach(self) -> None:
        """Отцепиться, не трогая сам браузер."""
        try:
            self._pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Работа со страницей Flow
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
    raise RuntimeError(f"Не нашёл поле ввода промта. Последняя ошибка: {last_err}")


def flatten_prompt(prompt: str) -> str:
    """Свести промт в одну строку.

    В композере Flow Enter = отправка, поэтому набирать текст с переводами
    строки нельзя: запрос уйдёт на первой же пустой строке. Абзацы склеиваем
    через ' | ', обычные переносы — через пробел, смысл промта не меняется.
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
        log("Промт свёрнут в одну строку (Enter в композере = отправка)")
    log(f"Промт вставлен ({len(flat)} символов)")


# ---------------------------------------------------------------------------
# Референсы: вставка картинки в композер до генерации
# ---------------------------------------------------------------------------


def load_reference(src: str | Path) -> tuple[bytes, str, str]:
    """Привести источник картинки к (байты, mime, имя файла).

    Принимает: путь к файлу, data:-URI, http(s)-ссылку или голый base64 —
    чтобы модель могла отдать картинку в любом удобном ей виде.
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
        # Сигнатура файла надёжнее mimetypes: на Windows он берёт типы из
        # реестра и, например, для .webp может вернуть image/png.
        mime = sniff_mime(data)
        if mime == "application/octet-stream":
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return data, mime, path.name

    # последний вариант: голый base64 без префикса
    if len(text) > 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", text or " "):
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileNotFoundError(f"Референс не распознан: {text[:60]}...") from exc
        return data, sniff_mime(data), "ref.png"

    raise FileNotFoundError(f"Файл референса не найден: {text}")


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
    """Положить байты во временный файл — нужен для системного буфера и upload."""
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
    """Положить картинку в системный буфер обмена Windows (PNG + Bitmap)."""
    if sys.platform != "win32":
        raise RuntimeError("Системный буфер поддержан только на Windows")
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
    """Настоящий Ctrl+V из системного буфера.

    Обычный keyboard.press('Control+V') через CDP не запускает команду вставки
    в редакторе — нужен Input.dispatchKeyEvent с commands: ['paste'].
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
    """Дождаться, пока референс догрузится на сайт.

    Готовность = появился новый чип-миниатюра, он отрисован (naturalWidth>0),
    спиннеров нет и набор чипов не меняется несколько опросов подряд.
    Возвращает url чипа или None, если ничего не появилось.
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
                log(f"Референс догрузился ({len(new)} миниатюр на композере)")
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
    """Вставить картинку-референс в композер Flow и дождаться её загрузки.

    Стратегии по порядку (`method='auto'`):
      paste     — синтетический ClipboardEvent с файлом (быстро, без буфера ОС);
      clipboard — картинка в системный буфер + настоящий Ctrl+V через CDP;
      drop      — синтетический drag&drop файла в композер;
      upload    — прямая подстановка файла в input[type=file].
    """
    data, mime, name = load_reference(src)
    b64 = base64.b64encode(data).decode("ascii")
    log(f"Референс: {name} ({len(data) // 1024} KB, {mime})")

    box = find_prompt_box(page, action_timeout)
    try:
        box.click()
    except Exception:
        pass

    # Метка на поле ввода: по ней JS находит цель для paste/drop.
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
                    log("  Ctrl+V из системного буфера отправлен")

                elif strategy == "drop":
                    page.evaluate(JS_DROP_FILE, [b64, mime, name, target_sel])
                    log("  drag&drop отправлен")

                elif strategy == "upload":
                    tmp = tmp or _temp_ref_file(data, name)
                    inputs = page.locator('input[type="file"]')
                    if not inputs.count():
                        raise RuntimeError("input[type=file] на странице нет")
                    inputs.last.set_input_files(str(tmp))
                    log("  файл подставлен в input[type=file]")

                else:
                    raise ValueError(f"Неизвестный метод вставки: {strategy}")

            except Exception as exc:
                msg = str(exc).splitlines()[0]
                errors.append(f"{strategy}: {msg}")
                log(f"  метод «{strategy}» не сработал: {msg}")
                continue

            thumb = wait_ref_uploaded(page, baseline, timeout, thumb_max=thumb_max)
            if thumb:
                return {"ok": True, "method": strategy, "thumb": thumb, "name": name}

            errors.append(f"{strategy}: миниатюра не появилась за {timeout} с")
            log(f"  метод «{strategy}»: миниатюра так и не появилась")

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
        log(f"=== Референс {i} ===")
        results.append(
            attach_reference(page, ref, method, timeout, action_timeout, thumb_max)
        )
    return results


def _submit_candidates(page: Page):
    """Все видимые кнопки-кандидаты на 'генерировать', с их локаторами."""
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
    """Найти кнопку генерации. wait_enabled — сколько сек ждать, пока станет активной."""
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

    return first_seen  # найдена, но так и не активна (или None)


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
                log(f"Нажал кнопку генерации [{text.replace(chr(10), ' | ')}] ({sel})")
                return
            log(f"Кнопка [{text.replace(chr(10), ' | ')}] осталась неактивной — жму Enter")
        except Exception as exc:
            log(f"Клик не прошёл ({exc}) — жму Enter")
    else:
        log("Кнопку не нашёл — жму Enter")
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
    """Ждать count новых картинок и стабилизации набора (чтобы не поймать превью)."""
    baseline = set(baseline)
    deadline = time.time() + timeout
    stable = 0
    prev_new: list[str] = []

    while time.time() < deadline:
        new = [u for u in collect_images(page, min_side) if u not in baseline]

        if new == prev_new and len(new) >= count:
            stable += 1
            if stable >= stable_polls:
                log(f"Готово: {len(new)} новых картинок")
                return new[:count] if count > 0 else new
        else:
            stable = 0

        if new != prev_new:
            log(f"Новых картинок: {len(new)}/{count}")
        prev_new = new
        page.wait_for_timeout(2000)

    if prev_new:
        log(f"Таймаут, но нашлось {len(prev_new)} картинок — качаю их")
        return prev_new
    raise TimeoutError("Не дождался сгенерированных картинок")


# ---------------------------------------------------------------------------
# Скачивание
# ---------------------------------------------------------------------------


# --- скачивание через меню карточки: more_vert -> Скачать -> 1K/2K/4K --------

# Пометить карточку, содержащую картинку с данным src, атрибутом data-flowdl.
# Так её можно взять обычным CSS-селектором без экранирования URL.
JS_MARK_CARD = """
(url) => {
  for (const el of document.querySelectorAll('[data-flowdl]')) el.removeAttribute('data-flowdl');
  let img = null;
  for (const cand of document.querySelectorAll('img')) {
    if ((cand.currentSrc || cand.src) === url) { img = cand; break; }
  }
  if (!img) return null;

  // Кнопки favorite/redo/more_vert живут в оверлее карточки, который
  // появляется по hover. Ищем предка с role="button", иначе самый крупный.
  let node = img, best = null;
  for (let i = 0; i < 10 && node; i++) {
    node = node.parentElement;
    if (!node) break;
    const r = node.getBoundingClientRect();
    if (r.width < 200 || r.height < 150) continue;
    if (!best) best = node;
    if (node.getAttribute('role') === 'button') { best = node; break; }
    if (r.width > 900) break;  // ушли в сетку целиком
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


# Тосты «Повышение разрешения завершено» висят в правом верхнем углу и
# перекрывают карточки третьей колонки — hover до них не доходит.
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
  if (!top) return { inside: false, what: 'ничего (вне окна)' };
  return {
    inside: card.contains(top) || top === card,
    what: (top.innerText || top.tagName).trim().replace(/\\n/g, ' '),
  };
}
"""


def dismiss_toasts(page: Page) -> None:
    """Убрать всплывающие уведомления с пути курсора."""
    # сначала штатная кнопка «Закрыть», если есть
    try:
        close_btns = page.locator('li button:has-text("Закрыть"), li button:has-text("Close")')
        for i in range(min(close_btns.count(), 5)):
            btn = close_btns.nth(i)
            if btn.is_visible():
                btn.click(timeout=1500)
    except Exception:
        pass
    # затем снимаем перехват мыши у оставшихся
    try:
        page.evaluate(JS_DISMISS_TOASTS)
    except Exception:
        pass


def close_menus(page: Page, timeout: int = 3000) -> None:
    """Закрыть открытые меню и дождаться их исчезновения."""
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
    """Кнопка more_vert, попадающая внутрь прямоугольника карточки.

    В шапке страницы тоже есть more_vert, поэтому фильтруем по геометрии.
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
    """Скачать картинку меню Flow в нужном разрешении. None — если не вышло."""
    label = QUALITY_LABELS.get(quality, "2K")

    rect = page.evaluate(JS_MARK_CARD, url)
    if not rect:
        log(f"  [{index}] карточка для картинки не найдена")
        return None

    more = None
    for attempt in range(1, 4):
        close_menus(page)
        dismiss_toasts(page)
        page.wait_for_timeout(300)  # доскроллиться / закрыть прошлое меню

        rect = page.evaluate(
            "() => { const e = document.querySelector('[data-flowdl=\"1\"]');"
            "  if (!e) return null; const r = e.getBoundingClientRect();"
            "  return {x: r.x, y: r.y, w: r.width, h: r.height}; }"
        )
        if not rect:
            return None

        cx, cy = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2

        # что реально лежит под точкой наведения
        blocker = page.evaluate(JS_HIT_TEST, [cx, cy])
        if blocker and not blocker["inside"]:
            log(f"  [{index}] карточку перекрывает: {blocker['what'][:50]} — сдвигаю")
            page.mouse.wheel(0, 140 if attempt == 1 else -140)
            page.wait_for_timeout(500)
            continue

        # курсор сначала уводим, иначе mouseenter не срабатывает повторно
        page.mouse.move(5, 5)
        page.wait_for_timeout(150)
        page.mouse.move(cx, cy, steps=8)
        page.wait_for_timeout(250)
        page.mouse.move(cx + 4, cy + 4)  # шевеление добивает hover-состояние
        page.wait_for_timeout(600)

        more = find_more_button_in(page, rect)
        if more is not None:
            break
        log(f"  [{index}] оверлей карточки не появился (попытка {attempt}/3)")

    if more is None:
        log(f"  [{index}] кнопка «Ещё» на карточке не появилась")
        return None
    more.click()
    page.wait_for_timeout(600)

    dl_item = page.locator(DOWNLOAD_ITEM_SEL).first
    dl_item.wait_for(state="visible", timeout=10_000)
    dl_item.hover()
    page.wait_for_timeout(700)

    # пункт подменю: "2K | Увеличенное разрешение"
    quality_item = page.locator(f'[role="menuitem"]:has-text("{label}")').last
    quality_item.wait_for(state="visible", timeout=10_000)

    log(f"  [{index}] жму «{label}» (апскейл может занять до {timeout} с)")
    try:
        with page.expect_download(timeout=timeout * 1000) as dl_info:
            quality_item.click()
        download = dl_info.value
        suffix = Path(download.suggested_filename).suffix or ".png"
        path = out_dir / f"{prefix}_{index:02d}_{label.lower()}{suffix}"
        download.save_as(str(path))
        return path
    except Exception as exc:
        log(f"  [{index}] загрузка через меню не удалась: {str(exc).splitlines()[0]}")
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
    """blob:/data: — читаем в самой странице; http(s) — через куки её контекста."""
    if url.startswith(("blob:", "data:")):
        res = page.evaluate(JS_FETCH_AS_B64, url)
        return base64.b64decode(res["b64"]), res.get("type", "")

    resp = page.context.request.get(url, timeout=120_000)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}")
    return resp.body(), resp.headers.get("content-type", "")


def upgrade_url(url: str) -> str:
    """googleusercontent: снять даунсайз-суффикс (=w512-h512 -> =s0)."""
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
        # основной путь: меню карточки -> Скачать -> 2K (с апскейлом)
        if quality in QUALITY_LABELS:
            try:
                path = download_via_ui(page, url, out_dir, prefix, i, quality, ui_timeout)
            except Exception as exc:
                log(f"  [{i}] ошибка UI-скачивания: {str(exc).splitlines()[0]}")
                path = None
            if path:
                seen_hashes.add(hashlib.sha1(path.read_bytes()).hexdigest())
                saved.append(path)
                log(f"  [{i}] сохранено: {path.name} ({path.stat().st_size // 1024} KB)")
                continue
            log(f"  [{i}] откатываюсь на прямое скачивание из src")

        for candidate in dict.fromkeys([upgrade_url(url), url]):
            try:
                data, ctype = fetch_bytes(page, candidate)
            except Exception as exc:
                log(f"  [{i}] не скачалось ({exc}) — пробую другой вариант URL")
                continue

            digest = hashlib.sha1(data).hexdigest()
            if digest in seen_hashes:
                log(f"  [{i}] дубликат, пропускаю")
                break
            seen_hashes.add(digest)

            path = out_dir / f"{prefix}_{i:02d}{ext_from(ctype, candidate)}"
            path.write_bytes(data)
            saved.append(path)
            log(f"  [{i}] сохранено: {path.name} ({len(data) // 1024} KB)")
            break
        else:
            log(f"  [{i}] ПРОПУЩЕНО: {url[:120]}")

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
    raise SystemExit("Нужен --prompt или --prompts-file")


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "_", text, flags=re.U).strip("_")
    return (s[:limit] or "gen").lower()


# ---------------------------------------------------------------------------
# Высокоуровневый API (его же зовёт MCP-сервер)
# ---------------------------------------------------------------------------


def image_size(path: Path) -> tuple[int, int] | None:
    """Размеры JPEG/PNG без внешних зависимостей."""
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":  # JPEG: ищем SOFn
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
    """Полный цикл: промт -> (референсы) -> генерация -> ожидание -> скачивание.

    Блокирует выполнение, пока все `count` картинок не будут сохранены.
    Если переданы `references`, картинки вставляются в композер (как Ctrl+V),
    скрипт ждёт окончания их загрузки на сайт и только потом жмёт «генерировать».
    Возвращает словарь с путями и метаданными — им пользуется MCP-сервер.
    """
    started = time.time()
    chrome = AttachedChrome(resolve_endpoint(cdp), action_timeout)
    try:
        page = chrome.find_flow_page(url)

        if "accounts.google.com" in page.url:
            return {
                "ok": False,
                "error": "not_logged_in",
                "message": "Chrome открыт на странице входа Google. Нужен ручной вход.",
                "images": [],
            }

        try:
            type_prompt(page, prompt, action_timeout)
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": "no_prompt_box",
                "message": (
                    f"{exc} Похоже, открыт лендинг Flow, а не проект — "
                    f"открой проект в этой вкладке ({page.url})."
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
                        f"Не удалось вставить {len(failed)} из {len(refs)} референсов "
                        "— генерация не запускалась. "
                        + "; ".join("/".join(f.get("errors", [])) for f in failed)[:400]
                    ),
                    "references": ref_results,
                    "images": [],
                }

        # Базовый набор снимаем ПОСЛЕ вставки референсов: иначе миниатюра
        # референса может попасть в «новые картинки».
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
                f"Сохранено {len(saved)} из {count} картинок в {folder}"
                if len(saved) == count
                else f"ВНИМАНИЕ: сохранено только {len(saved)} из {count}. Папка: {folder}"
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
    """Только вставить референсы в композер Flow, без генерации.

    Нужно, чтобы проверить, что вставка работает, не тратя квоту.
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
                    f"{exc} Похоже, открыт лендинг Flow, а не проект — "
                    f"открой проект в этой вкладке ({page.url})."
                ),
            }
        ok = all(r["ok"] for r in results) and bool(results)
        return {
            "ok": ok,
            "references": results,
            "message": (
                "Референсы на месте, композер готов к генерации"
                if ok
                else "Часть референсов не вставилась — смотри errors"
            ),
        }
    finally:
        chrome.detach()


def check_environment(cdp: str | None = None) -> dict:
    """Готов ли Chrome: порт отладки, вкладка Flow, логин."""
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
                f"Готово. Проект Flow: {flow[0]['url']}"
                if flow
                else "Открой проект Flow в отладочном Chrome (или залогинься)."
            ),
        }
    finally:
        chrome.detach()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Генерация картинок в Google Flow через уже открытый Chrome"
    )
    p.add_argument("--prompt", help="Текст промта")
    p.add_argument("--prompts-file", help="Файл с промтами, разделитель — строка '---'")
    p.add_argument("--url", help="URL проекта Flow (если вкладка ещё не открыта)")
    p.add_argument("--out", default="output", help="Папка для картинок")
    p.add_argument("--count", type=int, default=4, help="Сколько картинок ждать (0 = все новые)")
    p.add_argument("--cdp", help=f"CDP endpoint или порт (по умолчанию перебор {DEFAULT_PORTS})")
    p.add_argument("--list-tabs", action="store_true", help="Показать вкладки и выйти")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Проверить селекторы: вставить промт, найти кнопку, НЕ генерировать",
    )
    p.add_argument("--gen-timeout", type=int, default=600, help="Ожидание генерации, сек")
    p.add_argument("--action-timeout", type=int, default=60, help="Таймаут действий, сек")
    p.add_argument("--min-side", type=int, default=256, help="Мин. сторона картинки, px")
    p.add_argument(
        "--quality",
        choices=["2k", "1k", "4k", "src"],
        default="2k",
        help="Разрешение: 2k (апскейл, по умолчанию), 1k, 4k (нужен тариф), "
        "src — быстрый прямой забор из <img> без меню",
    )
    p.add_argument(
        "--ui-timeout", type=int, default=180, help="Ожидание апскейла+загрузки, сек"
    )
    p.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="PATH",
        help="Картинка-референс: путь к файлу, data:-URI или ссылка. "
        "Можно указать несколько раз. Вставляется в композер до генерации",
    )
    p.add_argument(
        "--ref-method",
        choices=["auto", "paste", "clipboard", "drop", "upload"],
        default="auto",
        help="Как вставлять референс: auto — перебор, clipboard — настоящий Ctrl+V "
        "через системный буфер Windows",
    )
    p.add_argument(
        "--ref-timeout", type=int, default=90, help="Ожидание загрузки референса, сек"
    )
    p.add_argument(
        "--ref-thumb-max",
        type=int,
        default=REF_THUMB_MAX_SIDE,
        help=f"Макс. сторона миниатюры референса, px ({REF_THUMB_MAX_SIDE}). "
        "Поднять, если Flow рисует чип крупнее и загрузка «не детектится»",
    )
    p.add_argument(
        "--ref-optional",
        action="store_true",
        help="Генерировать даже если референс не вставился (по умолчанию — стоп)",
    )
    p.add_argument("--settle", type=int, default=3, help="Пауза перед стартом, сек")
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
            input("Нужен вход в Google. Залогинься в браузере и нажми Enter здесь...")

        total = 0
        for idx, prompt in enumerate(prompts, 1):
            log(f"=== Промт {idx}/{len(prompts)} ===")

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
                    log("ОШИБКА: референс не вставился — генерацию не запускаю "
                        "(--ref-optional, чтобы всё равно генерировать)")
                    clear_prompt(page, args.action_timeout)
                    continue

            # базовый набор — после референсов, иначе миниатюра попадёт в «новые»
            baseline = set(collect_images(page, args.min_side))
            log(f"Картинок на странице до генерации: {len(baseline)}")

            if args.dry_run:
                found = find_submit(page, wait_enabled=10)
                if found:
                    sel, item, text = found
                    state = "активна" if item.is_enabled() else "НЕАКТИВНА"
                    log(f"DRY-RUN: кнопка [{text.replace(chr(10), ' | ')}] {state}, селектор {sel}")
                else:
                    log("DRY-RUN: кнопка НЕ найдена (будет Enter)")
                clear_prompt(page, args.action_timeout)
                log("DRY-RUN: поле очищено, генерация не запускалась")
                continue

            click_generate(page, wait_enabled=30 if args.ref else 15)

            try:
                urls = wait_for_new_images(
                    page, baseline, args.count, args.gen_timeout, args.min_side
                )
            except (TimeoutError, PWTimeout) as exc:
                log(f"ОШИБКА: {exc}")
                continue

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            folder = out_root / f"{stamp}_{slugify(prompt)}"
            saved = download_all(
                page, urls, folder, "img", quality=args.quality, ui_timeout=args.ui_timeout
            )
            total += len(saved)
            log(f"Промт {idx}: сохранено {len(saved)} шт. -> {folder}")

        log(f"ИТОГО: {total} картинок в {out_root}")
        return 0
    finally:
        chrome.detach()  # браузер остаётся открытым


if __name__ == "__main__":
    sys.exit(main())
