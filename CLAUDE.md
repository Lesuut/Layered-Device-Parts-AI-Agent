# Device Generator

A pipeline that builds 2D device assets for a game about taking things apart
and putting them back together (a phone comes apart layer by layer: front
shell → keypad → board → battery → back cover; parts get repaired and
reassembled).

**Main rule:** when the user hands over a picture of a device and asks for an
asset — work through the `device-asset-pipeline` skill
(`.claude/skills/device-asset-pipeline/`). It holds the step-by-step pipeline,
the criteria for picking images and parts, and the JSON format.

## Layout

```
Image Geneartor MCP/     image generation in Google Flow through a live Chrome
BG Remover MCP/          background removal -> PNG with transparency (local)
Asset Builder MCP/       part extraction, atlas, assembly, viewer.html
Pipeline Dashboard/      live progress panel, opens itself when a session starts
Prompts/                 pipeline prompts (do NOT edit unless asked)
Parts Library/           storage for EVERY part ever cut (including leftovers)
work/<device>/           intermediate pipeline files
assets/<device>/         finished assets: texture.png + device.json + preview
                         + OPEN_<device>.html (asset baked in, click and look)
                         + viewer.html (drop any other asset onto it)
PART_TYPES.md            registry of shared part types (the type field in device.json)
.claude/skills/          project skills
```

## MCP servers (registered in user scope)

| Server | Tools |
|---|---|
| `flow-images` | `flow_check`, `flow_setup`, `flow_generate`, `flow_paste_reference`, `flow_list_files` |
| `bg-remover` | `bg_check`, `bg_remove`, `bg_inspect`, `bg_punch`, `bg_shrink`, `bg_install_ai` |
| `asset-builder` | `asset_extract`, `asset_punch`, `asset_pack`, `asset_render`, `asset_update`, `asset_validate`, `asset_viewer`, `asset_package` |

Everything runs locally except generation: that goes through a real Chrome with
Google Flow open (CDP), signed in as the user.

## Progress panel

`Pipeline Dashboard/` — a live HTML panel on http://127.0.0.1:7788. The
`SessionStart` hook brings the server up and opens a browser window; the
`PreToolUse`/`PostToolUse` hooks write every tool call into
`Pipeline Dashboard/state/events.jsonl`. The server sorts the feed into the 13
pipeline steps, collects an image gallery and serves `/api/state`; the page
polls it every 700 ms.

The panel is only a mirror. Nothing has to be done specially for it: it sees
generations, image views, background removal, hole marks, part extraction and
assembly iterations on its own. The hooks are configured in
`.claude/settings.json`.

**Always open the panel — first thing, before any pipeline step.** Do not wait
for the `SessionStart` hook and do not assume it is already up: the session may
have started from another folder, the hook may not have fired, or the user may
have closed the window. One command, safe to run again:

```
py "Pipeline Dashboard/server.py" --ensure --open   ; start if missing, then open a window
py "Pipeline Dashboard/server.py" --status          ; check whether it is alive
```

`--ensure` does not spawn a second server, `--open` opens a tab. If it does not
come up — tell the user and keep working; the panel does not block the pipeline.

## What matters

- **Always take the prompt from disk.** Before every `flow_generate` — a fresh
  `Read` of the file in `Prompts/`, then send its text verbatim. The user edits
  these files by hand, so the copy in context goes stale. The `promptguard.py`
  hook compares the prompt against the files and asks for confirmation when
  they differ.
- **Flow quota is finite.** Every `flow_generate` spends the user's
  generations. Do not run them "just to try", do not restart on a hunch — two
  retries at most, and only with an explanation of why.
- **A reference is mandatory**: `flow_generate(reference_images=[...])` pastes
  the picture into the composer, waits for it to upload and only then presses
  "Create".
- **If the user gave no picture — draw the source yourself.** No need to ask
  for one. Take `Prompts/1 Devices View Generator.txt` verbatim, append which
  device is needed, generate a batch, **look at it and pick the best
  specimen**, crop it out of the sheet **into a square** (device centred, white
  margins) and put it in `work/<device>/source.png`. From there that square is
  an ordinary style anchor: it goes as a reference into the burger, into the
  layout and into any extra generation, and the finished assembly is checked
  against it.
- **Debug Chrome** lives on ports 9222/9223/9224; port 9222 is often taken by
  Lens Studio on this machine — the scripts detect that and move to the next one.
- **If debug Chrome is not running, bring it up yourself, do not wait for the
  user.** `flow_check` returned `ready: false` because the debug port is
  missing (no CDP, "port not responding") — call `flow_setup` right away: it
  starts a separate Chrome process with the `ChromeDebugProfile` profile, the
  user's normal Chrome does not have to be closed. If the `flow-images` MCP is
  unavailable, the same effect comes from `Image Geneartor MCP/start_chrome_debug.bat`
  (which runs `py run.py --setup-only`) — run it in the background, the bat
  ends on `pause`. Once it is up — `flow_check` again. The only thing worth
  stopping and asking the user about is what they do by hand: signing in to
  Google and opening the Flow project.
- **`bg_remove` keeps white inside the object** (`keep_holes`), so the cutouts
  for the screen and keys stay white. They are punched out by eye:
  `bg_inspect` draws a map with numbered blobs → look at it → `bg_punch` by the
  numbers of the real holes. Parts that are already extracted are finished off
  with `asset_punch`.
- **A white fringe along the contour** survives any cutout. `bg_shrink(px=2)`
  removes it — it eats 2 px inward along the whole boundary with transparency.
  Strictly the last step, after every punch.
- **Checking by eye is mandatory** at three steps: picking the best generation,
  picking parts off the contact sheet, and the final assembly. Never hand over
  an asset that has not been looked at.
- **A batch out of Flow is material, not a final answer.** Parts can be taken
  freely from different layouts; a single missing or broken part can be
  regenerated with your own prompt using the user's source picture as a
  reference; a non-essential part that ruins the assembly gets dropped — say so
  when it does. Details are in the skill.
- **Every extracted part piles up in `Parts Library/`.** Each piece out of
  `asset_extract` lands there — both the ones that went into the atlas and the
  rejects. A hook maintains it automatically (`Parts Library/library.py hook`
  on `asset_extract` and `asset_pack`); nothing has to be copied by hand.
  It all lies in one heap, not split per device — two folders, `used/` and
  `unused/`, with the device visible in the file name
  (`discman_part_003_2b1f2fe1.png`). Next to them sits the `index.json` registry
  (device, size, where it came from, `id`, `type`) and the contact sheets
  `_contact_used_NN.png` / `_contact_unused_NN.png`, 96 parts per sheet.
  **Before regenerating a part, look at the unused contact sheet first**: the
  shell, board or screen you need has often already been cut from a neighbouring
  device, and no generation is required. Commands:
  `py "Parts Library/library.py" stats`, `scan` (re-walk work/ and assets/),
  `rebuild` (recompute the marks), `sheet`.
- **Every part has a `type`** — a shared classification, the same across all
  devices. The registry of types lives in `PART_TYPES.md` in the root: read it
  first and take a fitting type from there, and only if nothing fits, add a new
  row to the table and then use it.
- **The viewer can edit the asset.** The "Editor" button: change layer (↑↓),
  trash and ✕ (cut a part out of the asset; layers are then renumbered
  consecutively with no gaps), type editing, Ctrl+Z, "Save device.json".
  Geometry is edited three ways: the `X`/`Y`/`W`/`H`/`R` fields in the list row
  (R is rotation in degrees), dragging the part around the scene, and the arrow
  keys (1 px, 10 with Shift). The "Type schema" checkbox draws callouts with
  types; a type's colour is derived from the hash of its name.
- **"Save" writes to the same file it was opened from**, no file explorer
  dialog pops up any more. Two paths: for a dropped asset the viewer holds a
  file handle and writes straight into it; for `OPEN_*.html` the path to
  `device.json` is baked in at packaging time, and the page hands the JSON to
  the panel server (`POST /api/save-device`), which puts the file back in place
  **and immediately rebuilds `OPEN_*.html`**. The server only writes inside
  `assets/` and `work/`, only into existing `.json` files, and only for
  requests from `file://` or localhost. If the panel is not running, the viewer
  falls back to the file picker dialog (once per session) and asks for a rebuild
  through `asset_viewer`.
- **Teardown test** — the button next to "Editor": a flat top-down view where
  the part under the cursor is grabbed with the mouse and pulled out like a
  jigsaw piece, with a "removed N of M" counter. It is a mode for checking the
  disassembly mechanic; it does not touch the asset — the offsets are kept
  separately and are reset by the "Reassemble" button, by double-clicking a
  part, or by leaving the mode.
- Python is invoked as `py` (not `python`). The shell is PowerShell 5.1:
  `&&` does not work, use `;`.

## Verified reference asset

`assets/nokia_3310/` — an assembled 7-layer asset built through this pipeline.
Good as a template for the `device.json` structure and as a test case for
`viewer.html`.
