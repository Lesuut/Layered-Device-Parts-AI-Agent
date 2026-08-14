# Flow Generator

Automating Google Flow: prompt → generation → downloading 4 pictures in 2K.
Runs **in an already open Chrome** (attached over CDP), the Google login is
yours.

Two ways to use it: a **CLI** (`RUN_TEST.bat`) and an **MCP server** for Claude
Code.

## What is inside

```
install.bat              installation: dependencies + MCP + skill
RUN_TEST.bat             manual run with a typed-in prompt
start_chrome_debug.bat   bring up debug Chrome
run.py                   launcher: Chrome, checks, interactive mode
flow_gen.py              all the logic for working with Flow
mcp_server.py            MCP server (4 tools)
skills/flow-image-gen/   the skill for Claude Code
mcp-config-example.json  config for installing by hand
```

## Installing as MCP (for Claude Code)

Double-click **`install.bat`** — it installs the dependencies, copies the skill
into `~/.claude/skills/flow-image-gen` and registers the server with
`claude mcp add flow-images --scope user`.

Then: run `RUN_TEST.bat` once, sign in to Google, open the Flow project.
Restart Claude Code and check `/mcp`.

The server's tools:

| Tool | What it does |
|---|---|
| `flow_check` | readiness: debug port, Flow tab, login |
| `flow_setup` | bring up debug Chrome |
| `flow_generate` | prompt (+ references) → generation → **waits for all 4** → downloads 2K, returns the paths |
| `flow_paste_reference` | only paste a reference into the composer, no generation |
| `flow_list_files` | show the generated batches |

`flow_generate` blocks: it returns only when every file is on disk (90–180 s for
4 pictures). The response is a JSON with `ok`, `downloaded`, `folder` and an
`images` list — per picture `path`, `width`, `height`, `bytes`, `upscaled_2k`.

## References (image-to-image)

A picture can be handed over as a sample — it gets pasted into the Flow composer
exactly the way a human does it with Ctrl+V, the script **waits for it to upload
to the site** and only then presses "Create".

```bash
python flow_gen.py --prompt "same device, exploded view" --ref C:\refs\phone.jpg
python flow_gen.py --prompt "..." --ref a.png --ref b.png     # several
python run.py --prompt "..." --ref C:\refs\phone.jpg
```

From Claude Code — the `reference_images` parameter of `flow_generate`: a list
of absolute paths, `data:` URIs or http(s) links.

The paste methods are tried in order (`--ref-method auto`):

| Method | How it works |
|---|---|
| `paste` | a synthetic `ClipboardEvent('paste')` with the file — no OS clipboard, the fastest |
| `clipboard` | the picture goes into the Windows clipboard (PNG+Bitmap) and a genuine Ctrl+V is sent over CDP `Input.dispatchKeyEvent` with `commands:["paste"]` |
| `drop` | a synthetic drag&drop of the file into the composer |
| `upload` | the file is put straight into `input[type=file]` |

The upload counts as finished when a new thumbnail has appeared, it is rendered
(`naturalWidth > 0`), there are no spinners on the page and the set of
thumbnails has not changed for 3 polls in a row. If the paste did not work,
**generation does not start** (the quota is intact) and the response holds
`error: reference_upload_failed`. To force generation without a reference:
`--ref-optional`.

## Installation

```bash
pip install -r requirements.txt
```

No browser has to be downloaded (`playwright install` is not needed) — the
script attaches to your Chrome instead of starting its own.

## Running

Double-click **`RUN_TEST.bat`** — the window stays open, every error is visible.
It checks Python and playwright (installing them if needed), brings Chrome up
with a debug port itself, lists the tabs, asks for a prompt, downloads the
pictures and opens the result folder.

**The normal Chrome does NOT have to be closed** — the automation runs in a
separate process with the `%LOCALAPPDATA%\ChromeDebugProfile` profile. The first
time, you have to sign in to Google in that window and open your Flow project —
after that the session is kept.

```bash
python run.py                       # interactive
python run.py --prompt "text"
python run.py --setup-only          # only bring Chrome up and sign in
```

## Running by hand (low level)

1. `python run.py --setup-only` (or `start_chrome_debug.bat`).
2. Open your Flow project in that Chrome.
3. Run the script:

```bash
python flow_gen.py --prompt "explode view of Nokia 3310, technical illustration"
python flow_gen.py --prompt "..." --out "C:\images" --count 4
python flow_gen.py --list-tabs            # check the connection
python flow_gen.py --prompts-file prompts.txt   # a batch of prompts
```

The pictures land in `output/<date_time>_<start_of_prompt>/img_01_2k.jpeg ...`

## About 2K

By default **2K with upscaling** is downloaded — not the direct `src` from the
gallery (that one gives 1200×896) but Flow's regular path: card → `⋮` →
"Download" → "2K. Higher resolution". The result is 2400×1792, ~2–4 MB. If the
menu did not open for some reason, the script falls back to a direct 1K download
so the picture is not lost (the log will hold the line "falling back to a direct
download from src").

4K needs a change of plan — Flow will show a stub itself.

## Flags

| Flag | Meaning |
|---|---|
| `--prompt` | the prompt text |
| `--prompts-file` | a file of prompts, separated by a `---` line |
| `--url` | URL of the Flow project, if the tab is not open |
| `--out` | output folder (`output` by default) |
| `--count` | how many pictures to wait for (4 by default, `0` = all new ones) |
| `--cdp` | port or endpoint (by default it tries 9222/9223/9224) |
| `--gen-timeout` | how long to wait for generation, sec (600 by default) |
| `--min-side` | min. picture side in px, filters out icons (256) |
| `--quality` | `2k` (default, 2400×1792 upscaled), `1k`, `4k` (needs a paid plan), `src` — a fast direct grab of 1200×896 with no menu |
| `--ui-timeout` | waiting for upscaling and download, sec (180) |
| `--ref` | a reference picture: a path, a `data:` URI or a link; can be repeated |
| `--ref-method` | `auto` (default), `paste`, `clipboard`, `drop`, `upload` |
| `--ref-timeout` | waiting for the reference to upload, sec (90) |
| `--ref-thumb-max` | max. side of the reference thumbnail, px (220) |
| `--ref-optional` | generate even if the reference was not pasted |
| `--dry-run` | check the selectors without spending a generation |
| `--list-tabs` | show the tabs and exit |

## If something is wrong

- **"No Chrome with an open debug port was found"** — Chrome is running without
  the flag. Close it completely and start it through the `.bat`.
- **Chrome 136+** does not allow debugging on the standard profile — that is why
  the `.bat` uses a separate `%LOCALAPPDATA%\ChromeDebugProfile` folder.
- **Port 9222 is taken by another application** (Lens Studio, for instance — it
  answers on `/json/version` too) — the script recognises that by the `Browser`
  string and moves on to 9223/9224 silently. In the log: "Skipping ...: this is
  not Chrome".
- **Could not find the input field / the button** — Flow changed its markup. The
  `PROMPT_SELECTORS` and `SUBMIT_SELECTORS` lists at the top of `flow_gen.py`
  are where to fix it.
- **The reference "did not finish uploading"** although the thumbnail is visible
  — Flow draws a chip larger than 220 px. Raise `--ref-thumb-max` (to 400, say).
- **The reference will not paste by any method** — check it separately with
  `python flow_gen.py --prompt "x" --ref f.png --ref-method clipboard`; the
  Chrome window has to be in the foreground for that.
- **Previews downloaded instead of the originals** — raise `--min-side` (to 512,
  say).
