# BG Remover

Local background removal: a picture → a PNG with transparency. Offline, no
clouds and no API keys. Two engines: an algorithmic one for a white background
and a local neural net for any.

Two ways to use it: a **CLI** and an **MCP server** for Claude Code.

## What is inside

```
install.bat            installation: dependencies + MCP + skill
bg_remove.py           background cutting: all the logic + CLI
refine.py              finishing: hole map, punching by mark, fringe cut + CLI
mcp_server.py          MCP server (6 tools)
skills/bg-remove/      the skill for Claude Code
requirements.txt       dependencies
```

## Engines

| Method | How it works | Speed (2400×1792) | What to install |
|---|---|---|---|
| `white` | a fill from the frame edges looks for connected white background; the edge is taken from the "whiteness" of the pixel, then the colour is unpremultiplied | ~1.5–2 s | pillow, numpy, scipy |
| `ai` | local segmentation with rembg (U²-Net / IS-Net, ONNX on CPU) | 5–15 s | rembg, onnxruntime + a ~180 MB model |
| `auto` | looks at the frame border: white → `white`, otherwise → `ai` | — | — |

An important property of `white`: only white **connected to the frame border**
counts as background. White parts inside the object, highlights and lettering
stay intact. Switched off with the `--no-keep-holes` flag (needed for
see-through holes).

No white fringe is left along the contour: semi-transparent pixels are
unpremultiplied back from "object colour over white" into the clean object
colour.

## Installation

Double-click **`install.bat`** — it installs the dependencies, asks about the
AI engine, copies the skill into `~/.claude/skills/bg-remove` and registers the
server (`claude mcp add bg-remover --scope user`).

By hand:

```bash
pip install -r requirements.txt
pip install rembg onnxruntime        # optional, the ai engine
python bg_remove.py --install-ai     # the same thing from the script
```

## CLI

```bash
python bg_remove.py in.jpeg                          # in_nobg.png lands next to it
python bg_remove.py in.jpeg --out C:\cut --trim      # crop the empty margins
python bg_remove.py "C:\images\*.jpeg" --method white
python bg_remove.py C:\images --recursive --out C:\cut
python bg_remove.py photo.jpg --method ai --ai-model human
python bg_remove.py --check                          # what is available on the system
python bg_remove.py in.jpeg --shrink 2               # cut the white fringe right away

python refine.py inspect cut.png                     # hole map: cut_holes.png/.json
python refine.py punch   cut.png --ids 1,4,7         # knock the holes out by number
python refine.py punch   cut.png --points 640x320    # ... or by your own point
python refine.py shrink  C:\parts --px 2             # cut the fringe off a whole folder
```

| Flag | Meaning |
|---|---|
| `--out` | output folder (next to the source by default) |
| `--method` | `auto` (default), `white`, `ai` |
| `--ai-model` | `general`, `u2net`, `human`, `anime`, `fast` |
| `--alpha-matting` | soft edge from the AI, noticeably slower |
| `--tol-bg` | the "this is background" threshold, 0..255 (12) — the main knob |
| `--tol-fg` | full-opacity threshold (45) |
| `--edge` | width of the soft edge, px (2) |
| `--feather` | alpha blur, sigma (0) |
| `--trim` / `--pad` | crop the transparent margins / padding when cropping |
| `--no-keep-holes` | knock out ALL white pixels, including the inner ones |
| `--suffix` | suffix of the output file name (`_nobg`) |
| `--compress` | PNG compression 0–9 (6); 9 gives −5% size but takes 8x longer |
| `--recursive` | walk folders recursively |
| `--json` | print the full JSON result to stdout |
| `--check` / `--install-ai` | environment / installing the AI |

Files with the `_nobg` suffix are skipped — our own output is not cut again.
Sources are never changed.

## MCP (Claude Code)

| Tool | What it does |
|---|---|
| `bg_check` | libraries, AI availability, downloaded models |
| `bg_remove` | files/folder/mask → PNG with transparency, returns the paths |
| `bg_inspect` | map of unpunched white blobs with numbers (PNG + JSON) |
| `bg_punch` | knock out white by numbers from the map or by your own points |
| `bg_shrink` | cut N px off the whole boundary — remove the white fringe |
| `bg_install_ai` | installing rembg+onnxruntime |

The full cycle: `bg_remove` → `bg_inspect` → the agent looks at the map with its
eyes → `bg_punch(ids=[...])` → `bg_shrink(px=2)`.

The `bg_remove` response holds, per file: `path`, `method`, `width`/`height`,
`bytes`, `opaque_share` (the share of opaque pixels) and `notes`.

## If the result is bad

- **The background is still there** (`opaque_share` ≈ 1) — the background is not
  perfectly white. Raise `--tol-bg` to 14–25 or use `--method ai`.
- **It ate the object** (`opaque_share` ≈ 0) — lower `--tol-bg` to 6–8.
- **White parts of the object disappeared** — check that `--no-keep-holes` is not
  set; if a part touches the frame edge, it counts as background.
- **A light fringe along the contour** — raise `--edge` to 3–4.
- **A ragged edge on a noisy JPEG** — `--feather 0.8`.
- **Shadows stayed semi-transparent** — that is by design; they can be removed
  completely by raising `--tol-bg`.

## The pipeline with the generator

Pictures out of `Image Geneartor MCP` (Google Flow) come on a white background,
so:

```bash
python bg_remove.py "..\Image Geneartor MCP\output\20260811_140250_*\*.jpeg" --method white --out C:\cut
```
