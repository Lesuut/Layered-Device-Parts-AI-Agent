---
name: flow-image-gen
description: Image generation in Google Flow (Nano Banana) through the flow-images MCP server. Use it when pictures have to be created from a text prompt or FROM A REFERENCE IMAGE (image-to-image, "make it in the style of this picture", "redo this photo"), to build a device layout (teardown/exploded view), concept art, an illustration or a set of variants of one image. Returns finished 2K files on disk.
---

# Generating pictures through Google Flow

The `flow-images` MCP server drives a real Chrome with Google Flow open: it
pastes the prompt, presses generate, waits for the result and downloads the
files in 2K.

## Tools

| Tool | Purpose |
|---|---|
| `flow_check` | Readiness: debug port, Flow tab, login. **Call this first.** |
| `flow_setup` | Bring up debug Chrome if it is not running |
| `flow_generate` | Prompt (+ references) → generation → waiting → download. The main tool |
| `flow_paste_reference` | Only paste a reference into the composer, no generation (no quota spent) |
| `flow_list_files` | What has already been generated and where it lies |

## Order of work

1. **`flow_check`.** If `ready: true` — generate right away.
2. If `reason: "chrome_not_running"` — call `flow_setup`, then `flow_check`
   again.
3. If `reason: "login_required"` or `"no_flow_tab"` — **stop and ask the user**
   to sign in to Google and open the Flow project in the debug Chrome. You
   cannot sign in yourself.
4. **`flow_generate`** with the prompt.

## Waiting: the call blocks

`flow_generate` returns control **only when every picture is ready and saved to
disk**. The waiting for generation and upscaling is already inside it.

- Typical time: **90–180 seconds** for 4 pictures in 2K.
- Do NOT call the tool again while the previous call has not returned.
- Do NOT poll `flow_list_files` in a loop waiting for files to appear.
- Do not tell the user it succeeded before you have seen a response with
  `ok: true`.

## What comes out

JSON. The key fields:

```json
{
  "ok": true,
  "requested": 4,
  "downloaded": 4,
  "folder": "...\\output\\20260810_235618_exploded_view_...",
  "elapsed_sec": 91.6,
  "images": [
    { "path": "...\\img_01_2k.jpeg", "filename": "img_01_2k.jpeg",
      "bytes": 3178616, "width": 2400, "height": 1792, "upscaled_2k": true }
  ]
}
```

**All 4 files come back in `images` — that is the complete list, nothing is left
"in progress".** Every element holds an absolute `path`, ready to be read,
copied or shown to the user.

Check the result like this:

- `ok: true` and `downloaded == requested` → everything is ready; list the file
  paths and the folder to the user.
- `ok: false` → look at `error`:
  - `not_logged_in` — ask the user to sign in to Google;
  - `generation_timeout` — Flow did not deliver the pictures in the time
    allowed; suggest a retry or raise `gen_timeout`.
- `downloaded < requested` → some pictures downloaded; say so honestly, name how
  many exactly and offer to generate the rest.
- `upscaled_2k: false` on a file → the fallback to 1K (1200×896) kicked in;
  that is not an error, but mention it in the report.

## Working from a reference (image-to-image)

If the user has a source picture ("make it in this style", "redo this photo",
"here is an example — draw something like it"), pass it in `reference_images`:

```json
{ "prompt": "exploded view in the same style, white background",
  "reference_images": ["C:\\Users\\...\\ref.png"] }
```

What the tool does under the hood:

1. Pastes the prompt into Flow's input field.
2. Pastes the picture into the composer — the same as Ctrl+V (a real paste
   event; on failure — the Windows system clipboard, drag&drop,
   `input[type=file]`).
3. **Waits for the picture to finish uploading to the site** — the thumbnail is
   rendered, there are no spinners, the set of chips is stable.
4. **Only then** presses "Create".

The rules:

- The format of `reference_images` is a list of strings: **absolute paths** to
  files, `data:image/png;base64,...` or http(s) links. Do not use relative
  paths.
- Your own picture (the one the model got in the conversation) — save it to a
  file first, then pass the path. Or hand over a data: URI directly.
- There can be several references — they get pasted one after another.
- If a reference could not be pasted, generation **does not start**:
  `error: "reference_upload_failed"` comes back and no quota is spent. Then show
  the user the `references[].errors` field and offer to check by hand through
  `flow_paste_reference`.
- `error: "no_prompt_box"` — the Flow landing page is open, not a project. Ask
  the user to open the project in the tab.
- The `flow_generate` response gains a `references` field — per reference,
  `name`, `ok`, `method` (which way it got pasted).

## Parameters

- `prompt` — the prompt text. English gives a steadier result than Russian.
- `reference_images` — the list of reference pictures (see above). None by
  default.
- `ref_method` — `auto` (the default: paste → clipboard → drop → upload). State
  it explicitly only while debugging a problem: `clipboard` — a genuine Ctrl+V
  through the Windows system clipboard, `upload` — through `input[type=file]`.
- `ref_timeout` — how long to wait for the reference to upload, sec (90).
- `count` — how many pictures to wait for, 4 by default (Flow is set to x4).
- `quality` — `2k` by default (2400×1792, with upscaling), `1k`, `src` (fast,
  1200×896, no menu), `4k` (needs a paid plan — it will hit a stub).
- `out_dir` — where to save. By default the `output` folder next to the server.
  If the user named a folder — pass it explicitly.
- `gen_timeout` (600 s), `ui_timeout` (180 s) — touch only on timeouts.

## Prompts

Nano Banana 2 is what runs. What gives a good result:

- English, specifics about the object and the style.
- For device layouts: `exploded view teardown of <object>, all internal parts
  laid out flat, technical illustration, light background`.
- Spell the background and the style out explicitly (`white background`,
  `technical illustration`, `isometric`, `studio lighting`) — otherwise Flow
  picks them itself.
- One call = one prompt = 4 variants of one idea. Different ideas need several
  sequential calls; do not try to glue them into one prompt.

## Limits

- Chrome with the `ChromeDebugProfile` profile has to be running and signed in.
  The user's normal Chrome does not have to be closed — this is a separate
  process.
- While generation is running, do not touch that Chrome tab with other tools.
- Every call spends the user's Google Flow quota. Do not run generation "just to
  try" without being asked — `flow_check` is there for checking that it works.
