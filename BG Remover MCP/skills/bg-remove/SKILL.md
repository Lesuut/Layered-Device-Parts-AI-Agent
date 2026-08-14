---
name: bg-remove
description: Local background removal for images through the bg-remover MCP server. Use it when asked to remove/cut out a background, make a transparent PNG, "cut out the object", "get rid of the white background", prepare an asset with an alpha channel, or when the background has to be cleaned off freshly generated pictures (from Google Flow, for instance). Works offline and writes finished PNGs with transparency to disk.
---

# Background removal, locally

The `bg-remover` MCP server cuts the background on the user's machine and saves
PNGs with an alpha channel. No internet needed (except the first download of the
AI model).

## Tools

| Tool | Purpose |
|---|---|
| `bg_check` | What is available: libraries, AI engine, downloaded models |
| `bg_remove` | The main one: files/folder/mask → PNG with transparency |
| `bg_inspect` | Map of unpunched white holes with numbers — **look at it with your eyes** |
| `bg_punch` | Knock out white by the numbers/points your eye picked |
| `bg_shrink` | Cut N px off the whole boundary — remove the white fringe |
| `bg_install_ai` | Install rembg+onnxruntime. **Only with the user's consent** |

The full cycle for an asset:

```
bg_remove → bg_inspect → [look at map_png] → bg_punch(ids=[...]) → bg_shrink(px=2)
```

A quick draft — just `bg_remove` (with `shrink: 2` right away if you like).

## Two engines

| Method | When | Speed | Requires |
|---|---|---|---|
| `white` | white/studio background, generations, product shots on white | ~1.5–2 s for 2400×1792 | nothing beyond pillow/numpy/scipy |
| `ai` | any background: photo, street, interior, complex object | 5–15 s on CPU | `rembg` + `onnxruntime` |
| `auto` | **the default**: looks at the frame border, white → `white`, otherwise → `ai` | — | — |

If the background is not white and the AI is not installed, `auto` does not
fail: it cuts as if white and writes a warning into `notes`. In that case offer
the user `bg_install_ai`.

## Order of work

1. First time in a session — `bg_check` (to find out whether `ai` is there).
2. `bg_remove` with a list of absolute paths.
3. Check `opaque_share` on every result, and `notes`.

## What comes out

```json
{
  "ok": true, "total": 4, "done": 4, "failed": 0, "elapsed_sec": 6.4,
  "results": [
    { "ok": true, "path": "...\\img_01_2k_nobg.png", "method": "white",
      "width": 2254, "height": 1676, "bytes": 3812134,
      "opaque_share": 0.4805, "trimmed_box": [73, 73, 2327, 1749],
      "elapsed_sec": 1.6, "notes": [] }
  ]
}
```

How to read the result:

- `opaque_share` — the share of non-transparent pixels. Healthy values are
  roughly 0.05–0.8. A guideline, not a verdict: a layout filling the frame
  honestly sits around 0.5.
- `opaque_share > 0.99` → the background was not found. Raise `tol_bg` (14–25)
  or switch to `method: "ai"`.
- `opaque_share < 0.01` → almost everything was cut away. Lower `tol_bg` (6–8).
- `notes` — warnings; show them to the user.
- `error: "ai_unavailable"` → the AI is not installed, offer `bg_install_ai` or
  `method: "white"`.

## Parameters

- `images` — a list: **absolute paths**, a whole folder or a mask
  (`C:\images\*.jpeg`). They can be mixed. Files with the `_nobg` suffix are
  skipped automatically — we do not chew our own output.
- `out_dir` — where to put them. Without it the PNGs land next to the sources.
- `method` — `auto` / `white` / `ai`.
- `trim` + `pad` — crop the transparent margins (for assets, icons, sprites).
  For a set of pictures of the same size **do not enable it**: the frames will
  drift apart.
- `tol_bg` (12) — what counts as background. The main knob when the result is
  bad.
- `tol_fg` (45), `edge` (2), `feather` (0) — edge softness. Rarely touched.
- `keep_holes` (true) — white areas not connected to the frame border stay
  opaque (highlights, white parts, lettering inside the object). Set it to
  `false` only when you need see-through holes, for example inside the letter
  "O" or a mug handle.
- `shrink` (0) — cut N px off the boundary right away. For asset parts set `2`.
- `ai_model` — `general` (default), `human` (people), `anime`, `fast`, `u2net`.

## Finishing: holes and the fringe

Two defects always remain, and both are cured after `bg_remove`.

**1. Unpunched holes.** `keep_holes` deliberately does not knock out white
inside the object — otherwise it would eat the white keys and the silkscreen on
the board. Because of that, genuine see-through holes (the screen window, holes
in the board, gaps between the shell ribs) stay filled with white.
Algorithmically a hole and a white part are indistinguishable — only the eye
decides:

1. `bg_inspect(image)` — finds every white opaque blob, writes
   `<name>_holes.png` (the object on a dark checker, blobs outlined and
   numbered) and `<name>_holes.json`.
2. **Open `map_png` and look.** The hints in the JSON help but do not decide:
   `enclosed: true` — the blob is surrounded by the body of the part on every
   side (usually a hole), `area_px` — large blobs are nearly always holes,
   `whiteness` closer to 0 — cleaner white. Keys, lettering, highlights — **do
   not touch**.
3. `bg_punch(image, ids=[1, 4, 7])` — from every mark the whole connected white
   area is flood-filled away. Add anything small you missed with your own
   points: `points: ["640x320"]` in the pixels of the **original**.
4. If the punch ate too much, the area leaked outward through a gap in the
   contour: repeat with `mode: "ball"` and a small `radius`, or lower `tol`.

**2. The white fringe.** A boundary pixel is half background and its colour
cannot be recovered any more — so it is simply thrown away.
`bg_shrink(images, px=2)` cuts 2 px inward everywhere opaque touches
transparent: both on the outer contour and around the punched holes. Pixels
butting against the frame border are not cut.

Do it as the **last** step: after `bg_punch`, otherwise a new hole brings its
own fringe. Watch `removed_share` — over a third means `px` is too large for a
thin part, go back to 1.

## Limits

- `white` cuts white background **connected to the frame border**. If the object
  touches the frame edge and is white itself, part of the object goes with the
  background.
- Shadows on white become semi-transparent rather than disappearing. If you need
  a clean edge, raise `tol_bg`.
- The output is always PNG (JPEG has no transparency). The file is noticeably
  larger than the source JPEG — that is normal.
- Sources are never touched; a new file is written next to them or into
  `out_dir`.

## Pairing with generation

The typical pipeline: `flow_generate` (the flow-image-gen skill) gives pictures
on a white background → hand their paths to `bg_remove` with `method: "white"` →
you get PNGs with transparency. For a per-part asset add `trim: true`.
