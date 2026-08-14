---
name: device-asset-pipeline
description: The full pipeline for building a 2D device asset for a take-apart/put-together game. Runs when the user gives a link or a path to a picture of a device (phone, player, console, camera and so on) and wants an asset — a parts atlas plus an assembly JSON. Also use it on phrases like "make an asset", "take this device apart", "generate the parts", "build a device sprite sheet", when reworking an already assembled asset, and for questions about how the pipeline is put together. It orchestrates three MCP servers: flow-images (generation), bg-remover (background removal), asset-builder (extraction, atlas, assembly).
---

# Pipeline: device photo → 2D game asset

The game: a device comes apart layer by layer (front shell → keypad → board →
battery → back cover), parts get cleaned/replaced, then it goes back together.
So the asset has to be **layered, top-down, with parts that do not overlap in
the atlas but stack into one whole device by their positions**.

## First: what kind of request is this

1. **Technical / project work** — code changes, questions, tooling. Do not run
   the pipeline, just work normally.
2. **There is a picture of a device** (a file path or a link) — run the whole
   pipeline below.

If it is not clear which one it is — ask in one sentence, do not invent.

## The main principle: you are the customer, not the receiving clerk

Flow returns 4 pictures at a time. **You are not obliged to pick from what you
were given.** A batch is material, not a final answer. At every step you have
four moves:

| Move | When |
|---|---|
| **take it** | the part is good and fits with the rest |
| **take it from another batch** | the part is bad in this batch and fine in the next one |
| **generate one more** | the part you need is in no batch at all, or is broken everywhere |
| **drop it** | the part is not critical and fighting with it costs too much |

The worst outcome is shipping an asset where a part is "sort of in place, but
wrong". Better one part fewer, and said out loud.

### The acceptance bar

An asset is done not when every part was found, but when **the assembled
picture is indistinguishable from the original device**. You did not "lay the
parts out roughly where they go" — you assembled a device. Three rules that are
not up for discussion:

1. **The assembly is one whole.** No gaps, no breaks, no "two separate objects
   next to each other" between the parts. On a clamshell the halves are joined
   by the hinge and form one silhouette; on a slider the halves overlap; on a
   candybar the shell is one piece. Two halves hanging apart with air between
   them are a defect, not "a clamshell in the open position".
2. **The insides are not visible at all when assembled.** No board, no battery,
   no membrane, no ribbon cable, no green edge peeking out from under the
   shell. An assembled asset looks like a whole device in a shop window — as if
   it had never been taken apart and were not made of parts at all. Only what
   is visible in the user's original picture is visible: shell, screen, keys,
   cover. Everything else is hidden under the upper layers.
3. **Every part is flat.** Not one isometric, not one "with volume". Details
   are in step 7.
4. **The screen is fitted by the screen, not by the part's bounding box.**
   Details are in step 10.
5. **The part matches its seat in shape.** A rectangular battery compartment
   takes a rectangular battery of the same proportion, not a round coin cell.
   A 4:3 screen window takes a 4:3 matrix. A round speaker socket takes a round
   speaker. A part that is "the same thing in principle, just a different
   shape" is the wrong part: swap it from the pool or generate a new one.

While even one rule is broken, `asset_package` does not get called. Not
"decent overall", not "nobody will notice in-game" — it gets redone until it is
whole. Shipping an assembly with a visible defect and writing about it in the
report is **not allowed**: the report explains missing parts, not a bad
assembly.

### The original picture is the reference right to the end

The picture the user gave is not only a style anchor for generation. It is the
**sample every assembly iteration is checked against**. Keep it at hand and
open it again on every "render → fix" round: with your eyes, next to the
preview, not from memory.

There is one question at check time: **if you put my assembly and the user's
picture side by side — is it the same device?** Not "similar", not "broadly
yes" — the same one: the same body proportions, the same screen shape and
place, the same keypad pattern, the same colours, the same silhouette. A
discrepancy means fixing the assembly, not explaining it in the report.

## Tool map

| Server | Tools | Role |
|---|---|---|
| `flow-images` | `flow_check`, `flow_generate` | generating pictures in Google Flow from a prompt + reference |
| `bg-remover` | `bg_check`, `bg_remove`, `bg_inspect`, `bg_punch`, `bg_shrink` | white background removal → PNG with alpha, punching holes by mark, trimming the fringe |
| `asset-builder` | `asset_extract`, `asset_punch`, `asset_pack`, `asset_render`, `asset_update`, `asset_validate`, `asset_viewer`, `asset_package` | part extraction, atlas, assembly, packaging |

The pipeline prompts live in `Prompts/` and must not be edited unless asked:
- `Prompts/2 Device to burger.txt` — device → isometric exploded view (the burger)
- `Prompts/3 Burger to parts.txt` — burger → flat top-down parts layout

**First attempt — the prompt off disk, verbatim.** The user edits these files by
hand, and edits them often: the text in your context from last time may already
be stale. Before every `flow_generate` for these two steps, `Read` the file you
need and send its content **verbatim** — nothing appended, nothing shortened,
nothing "improved".

**On a retry the prompt can and should be fixed.** If the first attempt came
out wrong (parts in isometry, halves that do not join, a screen fused to the
board), running the same text a second time is pointless. On a retry:

- start from the fresh text off disk;
- **append a targeted block for the specific problem** — what exactly broke and
  how it should be. Word it hard and in English, in the style of the rest of the
  prompt: "EVERY part without exception must be flat top-down — no part may
  show its side wall, thickness, or 3D volume", "the two clamshell halves must
  be drawn as one connected body joined by the hinge";
- delete nothing from the original text — only add;
- **do not touch the files in `Prompts/`**: the edit lives inside one call.
  Changing the file on disk happens only on an explicit request from the user;
- one line in the report: what did not work and what you appended.

The single-part prompt (step 8) you write yourself from scratch, there is no
file for it.

The `promptguard.py` hook compares the text being sent against the files in
`Prompts/` and, when they differ, sends the call to the user for confirmation.
On the first attempt of steps 2 and 4 it should not fire — if it does, the
prompt did not come off disk, re-read the file. On a retry with an appended
block and on step 8 firing is normal: confirm and explain what you added.

Working folders: intermediates in `work/<device>/`, finished assets in
`assets/<device>/`.

## Generation budget

Every `flow_generate` spends the user's quota; it is finite and it costs money.
The guideline per device is **up to 6 calls**:

- 1 — the burger
- 1 — the parts layout
- 2 — spare for regenerating a batch (burger or layout)
- 2 — spare for generating single parts

Fitting into 2-3 is great. About to ask for a seventh — first tell the user
what has been spent and what the extra one is for, and wait for an answer.
Explain every regeneration in one line: **what is wrong and what you are
changing**. Do not fire an identical call twice in a row hoping to get lucky:
if the prompt and the reference are the same, change at least one of them —
otherwise the result will be the same too.

---

# Steps

## 0. Environment and the style anchor

`flow_check`. Then act on the reason it refused:

- **No debug Chrome** (the debug port does not answer, no CDP) — bring it up
  yourself, no questions: `flow_setup`. It starts a separate Chrome with the
  `ChromeDebugProfile` profile and does not touch the user's normal Chrome. If
  the `flow-images` MCP is missing — the same launch via the bat in the
  background: `Image Geneartor MCP/start_chrome_debug.bat` (which runs
  `py run.py --setup-only`; the bat hangs on `pause`, so background only).
  Once it is up — repeat `flow_check`.
- **Not signed in to Google / the Flow project is not open** — that is the
  user's hands. Say exactly what needs doing and stop. You cannot sign in
  yourself.

Do not stop the pipeline over something that is fixed by starting a process.

### No picture from the user — draw it yourself

The user named the device in words ("make a Polaroid asset") and gave no
picture — **do not ask for one, draw the source yourself**. The order:

1. `Read` the file `Prompts/1 Devices View Generator.txt` — it is about a
   catalogue sheet of retro devices in the project's style.
2. Send its text **verbatim**, appending one line in English about the device
   you need: which one exactly, top/front view, large, one object in frame or
   several angles. There is no reference at this step — the prompt holds the
   style on its own. The `promptguard` hook will fire here: that is normal,
   explain what you appended.
3. Look at the batch **with your eyes** and pick the best specimen: correct
   device proportions, a clean flat view, no perspective, every control
   readable.
4. **Crop the chosen device into a square** (device centred, white margins
   around it, side taken from the larger dimension) and save it as
   `work/<device>/source.png`. The square keeps Flow from cropping or
   stretching the reference.
5. From there — the ordinary pipeline from step 1, where `source.png` plays the
   role of the user's original picture in everything: both as the style anchor
   and as the reference for checking.

This step costs one generation — budget it on top of the six.

**Remember the path to the user's original picture.** It is the style anchor:
it goes as the reference into the burger and into any single-part generation.
Every picture in the pipeline is its descendant, and only it guarantees that the
colour, the line weight and the character of the device do not drift. Keep the
path with you until the work is done and do not swap it for intermediate files.

## 1. The part spec — before any generation

Look at the original picture and **write down the list of parts this device
must have**. Not from what Flow will draw, but from meaning: a Tamagotchi is a
shell, a screen, a board, buttons, a battery; a player also has a tape
transport.

The format is three groups:

- **Core.** Without these it is not an asset. Usually: back cover,
  shell/chassis, board, screen (if there is one), controls, front shell.
- **Useful.** Improves the teardown: battery, battery contact, membrane,
  speaker/buzzer, compartment lid, a couple of screws.
- **Small stuff.** Springs, ribbon cables, coils, individual chips. Taken only
  if it fell into place by itself; nothing gets redone for it.

Lean on `references/part_taxonomy.md`: it has kits by device type, the layer
order and the signs of "do not take this part".

### Composite devices: clamshell, slider, book

If the device has **two moving halves**, decide right here how they will live in
the asset and write it into the spec. Otherwise the assembly ends up as two
separate objects in one file.

- **The halves count as one body.** Each has its own set of layers, but the
  silhouette on the canvas is shared. A clamshell is drawn open; the joint line
  runs along the hinge, and there must be no air between the halves.
- **The hinge is a core part, not small stuff.** `hinge` (type `mechanism`)
  physically ties the halves together and covers the joint. No hinge in any
  batch is a reason to generate one per step 8, not a reason to assemble it
  "spread apart".
- **The halves are the same width.** The upper half 15% wider than the lower one
  is a bad pick, not "that is how it was drawn": find another pair or generate
  one.
- **A flex cable through the hinge** (`flex_cable`) is taken if it turned up: it
  lies across the joint and stitches the picture together further.

Slider: the halves overlap, the upper lies on the lower, no gap at the joint.
Book/laptop: same as a clamshell.

The spec is a working checklist right to the end. At step 7 you report against
every line of it, at step 12 you check against it once more.

## 2. Device → burger

`Read` the file `Prompts/2 Device to burger.txt` — right now, not from memory.
Then `flow_generate` with that text verbatim,
`reference_images: [<style anchor>]`, `count: 4`.

The reference is mandatory — without it Flow will draw somebody else's device.

## 3. Judging the burger batch (with your eyes)

Read all 4 pictures through `Read` and judge them:

| Criterion | Bad | Good |
|---|---|---|
| Completeness of the teardown | parts fused together, shell still whole | every layer separate |
| Recognisability | a different device | the same as on the reference |
| Angle | strong perspective, parts side-on | even isometry, parts readable |
| Style | photorealism, noise | clean line art with flat fills |
| Text | title, numbers, callouts | a bare schematic |

Text being present is not fatal: it usually disappears at the next step. What is
fatal is fused parts and the wrong device.

**Decision.** There is a variant where the core of the spec is visible
separately — take it and move on. In none of them are the board or the screen
visible as separate parts — regenerate the batch (one attempt), telling the user
what was missing.

## 4. Burger → parts layout

`Read` again — now `Prompts/3 Burger to parts.txt`, even if you read it an hour
ago. Then `flow_generate` with that text verbatim,
`reference_images: [<chosen burger>]`, `count: 4`.

## 5. Judging the layouts — you may take several

Open all 4 through `Read`. The point here is **not to pick one picture but to
gather a pool of parts**. Judge picture by picture:

| Criterion | What to look at |
|---|---|
| Angle | strictly top-down; an isometric layout is useless, the prompt did not work |
| Separation | parts do not touch each other |
| Shell shapes | front and rear halves the same shape and size; a round back with an oval front is a defect |
| Board | fits inside the shell, cables do not stick far out past the outline |
| Screen | rectangular, separate, with no board fused to it |

**Two or three pictures may be usable — take them all.** Parts mix freely
between sources: what matters is not that one picture is consistent, but that
the parts fit each other in shape and size. The typical case: shell and buttons
from one layout, board from another — because in the first one the board's wires
stick out past the shell outline.

**The angle is judged picture by picture AND part by part.** Flow's typical
failure is not "the whole picture is isometric" but "the picture is flat, but
the battery, camera, speaker and connectors are drawn with volume". Such a
picture is not unusable as a whole: its flat parts can be taken, its volumetric
ones cannot — not a single one. Note this right away so you do not drag them
into step 7.

All four isometric, or the core of the spec falls apart in every one, or every
one has more volumetric parts than flat ones — **regenerate the batch with a
strengthened prompt** (the rules are in the prompt section above: the base text
off disk plus a targeted block about every part being flat). Tell the user what
exactly did not work and what you appended.

## 6. Background and holes

`bg_remove` on every usable picture, `method: "white"`,
`out_dir: work/<device>/cut`. Do NOT enable `trim`: the part coordinates will
still be needed. Do NOT enable `shrink` either: the fringe gets cut at the end.

Check `opaque_share`: 0.3–0.7 is normal. Close to 1 means the background did not
come off (usually it is not white but cream or greyish): raise `tol_bg` to 20–26
and repeat on that picture only.

**Punching the holes.** `keep_holes` deliberately leaves white inside the
object, otherwise it would eat the white keys and the silkscreen on the board.
That is why screen windows, holes in boards and gaps in the shell stay filled
with white. For every picture you are taking into extraction:

1. `bg_inspect(<path to _nobg.png>)`.
2. **Open `map_png` through `Read`.** The blobs are outlined and numbered.
3. Hand the numbers of the real holes to `bg_punch(image, ids=[...])`. The sign
   of a hole: `enclosed: true` plus common sense from the picture. Do not touch
   white PARTS (keys, membrane, battery label, lettering, highlights).
4. Small things `bg_inspect` did not find go in as points in the original's
   pixels: `points: ["640x320"]`.

Missing a hole is not a disaster: at step 9 `asset_punch` finishes it off on the
part itself.

## 7. Extraction and filling the spec

`asset_extract` **on all usable pictures at once** — they land in one pool with
shared numbering. **Be sure to open `contact_sheet.png` through `Read`**: every
part on it is labelled `#N` and with its size.

Now go through the spec from step 1 line by line and give each one a status:

- **have it** — the part number and which picture it came from;
- **have it, but bad** — what is wrong: fused to a neighbour, shown from the
  back, proportions that do not match the shell, a cable sticking out past the
  outline;
- **missing** — not found in any batch.

Selection rules:

- **Proportions beat beauty.** The board has to fit into the shell, the battery
  into its compartment, the buttons into the holes in the shell. A part that is
  beautiful but a third larger than the shell is "bad", not "have it".
- **Shape beats the name.** A part must suit its seat: a rectangular
  compartment takes a rectangular battery of the same proportion, a round socket
  takes a round speaker, a 4:3 window takes a 4:3 matrix. A round coin cell in
  place of a prismatic pack is the wrong part, not "a battery, just a different
  shape". Look at the seat in the shell first, then pick the part for it.
- Duplicates of one part in the pool — take the one that fits the already chosen
  parts better, not the larger one.
- Rubbish goes past: fragments of letters, shadows, frames, fused pairs, the
  fully assembled device.
- Give every part an `id` in Latin snake_case, a human-readable `name` and a
  `type` — a shared type per the rules below.

### Flatness: check every part, no exceptions

The asset is flat. **Not one isometric part can get into it** — not a small one,
not a "barely noticeable" one. A volumetric battery next to a flat shell breaks
the assembly worse than a missing battery does.

Before you put a part on the list, look at it on the contact sheet and answer:
**is this a strictly top-down view?** Signs that it is not, any one of them is
fatal:

- a **side face or an end** is visible — a rim on the battery, a cylinder on the
  camera, a barrel on the speaker, a wall on the connector;
- the part reads as a **box or a puck**, not as a silhouette;
- the **bottom or far edge is offset** relative to the top one (the sign of 30°
  isometry);
- a **self-shadow / volume gradient** inside the part instead of a flat fill;
- the outline is **bevelled diagonally** where a flat view would have a straight
  line.

Flow tips small parts into volume more often than large ones: battery, camera,
speaker, buzzer, tablet-shaped buttons, connectors, hinge, chips. Look at those
specifically — they are the ones that slip through.

What to do with a volumetric part:

| Part | Move |
|---|---|
| core (shell, board, screen, front panel) | replace with a flat duplicate from another layout; none — generate one per step 8 |
| useful (battery, membrane, speaker) | replace or generate; did not work — drop it and say so |
| small stuff | drop it at once, no deliberation |

"There is no flat duplicate and the part is needed" is not a reason to take the
volumetric one. It is a reason to go to step 8 with a prompt that says outright:
"strict orthographic top-down view, absolutely flat, no side walls, no
thickness, no 3D volume, no shadows".

### The part type

`type` is a classification shared across all devices: a Tamagotchi screen, a
Nokia display and a console matrix are all three `display`. The game treats
parts of one type the same way, and the viewer colours them on the schema (the
colour is derived from the hash of the name, so one type is always one colour).

The registry of types is **`PART_TYPES.md` in the project root**. The order of
work:

1. `Read` that file — before handing out any types.
2. For every part look for a fitting type among the ones written down. Found
   one — take it, even if the part is named differently.
3. Nothing fit — **first add the new type to the table in the file** (`type`,
   what it is, example ids), and only then use it.
4. Do not breed synonyms: `pcb` and `board` are one `circuit_board`. Two names
   for one thing give two colours on the schema and break the point.
5. A completely unclear part is `misc`.

The type goes into `asset_pack` alongside `id` and `name`, and is fixed
afterwards through `asset_update` with a `type` field.

Then decide by the statuses:

| What is left | What to do |
|---|---|
| the core is all "have it" | step 9 |
| the core has "missing" or "bad" | step 8 — generate that part |
| useful is "bad" | step 8 if the budget allows; otherwise drop it and say so |
| small stuff is "bad"/"missing" | drop it silently, mention it in the final report |

## 8. Generating a single part

**The storeroom first, generation second.** `Parts Library/` holds every piece
the pipeline ever cut, dumped in one heap across all devices: `used/` and
`unused/`. Open the sheets `_contact_unused_NN.png` and look with your eyes —
board, battery, speaker, flex cable and fasteners are often identical across
devices, and the file name says where a part came from. Found a suitable one —
take the file straight from the storeroom into `asset_pack` and spend no
generation. To see what is there: `py "Parts Library/library.py" stats`.

Nothing there — then generate. There is no prompt for this in `Prompts/`: **you
write the prompt yourself**, short and to the point.

What it has to contain:

1. **What is being drawn** — one part, named explicitly: "the main circuit board
   of this device", "the LCD display module", "the rear housing".
2. **The angle** — strictly orthographic top-down, flat, no isometry and no
   perspective.
3. **The style** — the same as on the reference: clean line art, flat fills, the
   same colours, no shadows.
4. **The background** — clean white, one part in frame, centred.
5. **The bans** — no text, labels, numbers, frames, shadows, and none of the
   device's other parts in frame.
6. **A fix for the specific problem** — the whole reason for doing this. The
   board did not fit into the shell: "proportions must match the device body, no
   cables or connectors extending beyond the board outline". The display was the
   wrong shape: "rectangular screen with straight edges, aspect ratio matching
   the window in the front shell".

**References:** always the user's original picture (the style anchor from step
0). If the part has to match an already chosen kit — add the layout the shell
came from as a second reference.

`count: 4` — the spread on a single part is wide, and out of four there is
usually a good one. Then as usual: `bg_remove` → if needed
`bg_inspect`/`bg_punch` → `asset_extract` into the same `work/<device>/parts` →
look at the contact sheet with your eyes → take the number you need.

If it did not work on the second attempt either, the part is not happening.
Core — tell the user straight that the asset is incomplete and why. Not core —
drop it and move on.

## 9. Cutouts and the fringe

`asset_punch` for shells and front panels: the screen cutout, the key holes and
the screw holes have to become see-through. Do NOT apply it to light parts
(white membrane, battery label). After it — look at the part with your eyes; a
`punched_share` above ~0.5 is suspicious.

Then `bg_shrink(images: [<the list of chosen parts>], px: 2)` — it removes the
light outline along every boundary with transparency, both on the outer contour
and around the punched holes.

**Strictly last** before the atlas: any punch after this brings its own fringe
back. Watch `removed_share`: over a third on a part means the part is thin —
redo it with `px: 1`.

## 10. The atlas

`asset_pack` with the list of parts in layer order (`layer` 0 = the very
bottom, every part gets its own layer number).

### Layer order is physical, not arbitrary

The layers repeat the real assembly of the device from the bottom up. It is
also the teardown order in the game, only reversed: the player removes from the
top down, and every removed layer has to reveal what genuinely lies beneath it.

The skeleton of the order, common to nearly every device:

1. **Bottom shell** — back cover, tub-shaped body. Always `layer` 0.
2. **Frame** — chassis, mid frame, shielding plates.
3. **Power** — battery, contacts.
4. **Boards** — main, daughter.
5. **Modules** — speaker, camera, vibration motor, hinge.
6. **Screen** — matrix, display module.
7. **Controls** — membrane, then keypad, then individual buttons.
8. **Front panels** — bezel, front cover. Always last.

The sanity check: a keypad cannot lie under the board, a battery cannot lie over
the front panel, the membrane is always under the keys and never over them. Once
the order is built, run your eyes down this list before calling `asset_pack`.

Set the starting positions sensibly, not all in the centre: back cover in the
middle of the canvas, battery below centre, board centred, controls in the lower
third, screen in the upper.

**Design the overlap in from the start.** The upper layer has to be slightly
larger than what it hides, otherwise the insides crawl out at the edges and you
spend iterations on something that packing solved. Make the board narrower than
the front panel window, the membrane narrower than the keypad, the battery
smaller than its compartment.

A useful trick: for a punched front panel you know the hole coordinates — they
were in the `bbox` of the regions from `bg_inspect`. Convert them into part
coordinates (minus the part's `src_bbox` from `asset_extract`) and put the
buttons and the screen straight onto those points instead of eyeballing it. It
saves two or three assembly iterations.

### The screen is measured by the screen, not by the part's bounding box

The most common assembly mistake. A `display` part is almost never equal to the
screen: it is a **module** — the glowing matrix plus the bezel around it, plus
the ribbon tongue at the bottom, sometimes plus a chunk of board. The part's
`size` is the bounding box of the whole module. Fit by that and the screen
inside the window comes out small, drifts upward, and the ribbon pokes into the
window from below.

The correct order:

1. **Measure the window** in the front panel: the `bbox` of the `bg_inspect`
   region you punched for the screen, converted into panel coordinates. That is
   `Wwin × Hwin` and the window centre on the canvas.
2. **Measure the matrix itself inside the part** — by eye off the part's
   picture: where the active screen area starts and ends. Write down its
   fraction of the part height: `k = Hmatrix / Hpart` (usually 0.6–0.8), the
   same fraction across the width, and **the offset of the matrix top from the
   part top** as a fraction `t`.
3. **Compute the part size from the window, not the other way round:**
   `Hpart = Hwin / k`, `Wpart = Wwin / kw`.
4. **Compute the position so the centres of the matrix and the window line up**,
   not the centres of the part and the window: part centre = window centre −
   (the offset of the matrix centre relative to the part centre, at the new
   scale).

The result: the matrix fills the window edge to edge, the module bezel and the
ribbon go under the front panel. **Background showing along the window edge and
a ribbon poking into the window are acceptance defects**, not "a minor thing":
the asset does not get packaged with them.

The same rule holds for any part whose working area is smaller than its bounding
box: a d-pad with a backing plate, a keypad with a skirt, a lens with a flange.

## 11. The assembly loop: render → fix → render

1. `asset_render` mode=`flat` → **open the preview through `Read`**, and **the
   user's original picture right after** — the check is done on two pictures
   side by side.
2. Go through the acceptance checklist below — all of it, point by point, not
   "broadly by eye".
3. `asset_update` with `dx`/`dy`/`size`/`rotation` — fix whatever has drifted.
4. Repeat until the checklist comes out clean.

### The acceptance checklist

Look at the preview picky, like at someone else's work you have to send back.
The job of looking is **to find a defect**, not to convince yourself it "broadly
resembles it". Every point is either "clean" or a specific fix. "Seems fine" is
"not clean".

| # | What you check | The defect looks like |
|---|---|---|
| 1 | **Silhouette is whole** | halves apart, a gap between parts, a part hanging in the air |
| 2 | **No insides visible** | board, green edge, battery, membrane or cable poking out from under the panel — even by a pixel |
| 3 | **The joint of the halves** | a strip of background visible between top and bottom; the hinge does not cover the joint |
| 4 | **Nothing sticks out past the outline** | a part crawls out past the outer contour of the shell |
| 5 | **The screen** | background showing along the window edge; the ribbon or module bezel visible in the window; the matrix smaller than the window |
| 6 | **Controls** | keys did not land in their holes, the d-pad is offset, a button is half under the bezel |
| 7 | **Shape versus seat** | a round battery in a rectangular compartment, a screen with a different aspect than the window |
| 8 | **Proportions** | halves of different widths; a part noticeably larger than its seat |
| 9 | **Flatness** | even one part reads as volumetric (see step 7) |
| 10 | **Layer order** | a lower part on top of an upper one, the panel under the keys, the membrane over the keys |
| 11 | **Clean edges** | a light fringe along the contour, ragged pixels, leftover background |
| 12 | **Is it the same device** | open the user's original picture next to the preview: silhouette, proportions, the shape and place of the screen, the keypad pattern, the colours |

Points 1, 2, 3, 5, 9 and 12 are blocking. With any of them open, `asset_package`
does not get called under any circumstances.

**Point 2 is the main one.** An assembled asset has to look like a whole device
nobody ever took apart: from the outside only shell, screen, keys and cover. Any
inside visible in the assembled state is a defect, even a narrow green strip
along the edge. It gets fixed by the size of the upper layer or the size of the
lower one, not by an explanation in the report.

**Point 12 is done with your eyes, not from memory.** Open the original picture
through `Read` again on every round and look at it next to
`preview_flat.png`. Checking from memory is not checking.

**Look at scale.** Defects in the joint, the edges and the screen are not
visible on the whole preview. If a point raises a doubt — open
`preview_flat.png` once more and look right at that spot, not at the picture in
general.

**Do not report on what you did not check.** "Assembled neatly" without going
through the checklist is a lie in the report.

**The three-iteration rule.** If a part still has not settled after three fixes,
the problem is not the position, it is the part. Stop moving it and choose:

- **replace it from the pool** — go back to the step 7 contact sheet and take
  the same part from another picture. You now know the exact size of the seat,
  and the second choice comes out far more accurate than the first;
- **generate one** — step 8, with the problem described in the prompt ("must fit
  inside the body outline", "rectangular, same aspect as the screen window");
- **drop it** — if the part is not core. A cable that crawls out past the shell
  at any size; a spring that reads as dirt; a screw with nowhere to go. A
  non-critical part that ruins the assembly is always worse than its absence.

Going back is a normal pipeline move, not a failure. What is bad is nudging a
broken part twenty times and shipping it as is.

**The three-iteration rule is not permission to give up.** It says "stop moving
it and change the part", not "leave it and write it up in the report". A core
part that will not settle drives you to step 8 for a new one, not to packaging.

**Act on your own.** Within the generation budget the decision to regenerate, to
generate extra or to swap a part is yours — you do not need permission for every
step, you need to report afterwards. You ask only when the budget is spent (the
seventh call) or when the work becomes pointless without the user's answer.

Handy: `asset_render` mode=`exploded` — the whole layer order at a glance.

## 12. Validation and packaging

Before packaging — three checks, all mandatory:

1. **The step 1 spec.** Is the whole core there? No — say so straight, do not
   pass an incomplete asset off as finished.
2. **The step 11 acceptance checklist.** Gone through in full, blocking points
   clean. Even one not clean — back into the assembly loop, not into packaging.
3. **Comparison with the original.** Open the user's original picture next to
   `preview_flat.png`. The assembly has to read as the same device: the same
   proportions, the same silhouette, the same colours. It does not read that way
   — fix it, do not ship it.

`asset_validate` — must come back `ok: true` (overlapping frames in the atlas
are not allowed). Then `asset_package` into `assets/<device>/`.

Show the user:

- the path to the folder, the number of parts, the atlas size;
- **`OPEN_<device>.html` opens on a double click** — the asset is baked inside,
  nothing has to be dragged in; `viewer.html` sits next to it for other assets;
- that it has an **editor**: the "Editor" button gives `↑↓` arrows for changing
  layer, a bin in the footer and an `✕` button (they cut a part out of the
  asset, after which layers are renumbered consecutively with no gaps), a type
  field on every row and `Ctrl+Z`. Part geometry is editable: the `X`/`Y` fields
  (centre on the canvas), `W`/`H` (size) and `R` (rotation in degrees) in the
  list row, dragging the part around the scene with the mouse, and the arrow
  keys — moving the selected part by 1 px (`Shift` — by 10);
- that the **"Save device.json" button writes back into the same file** with no
  explorer dialog: for `OPEN_*.html` the path is baked in at packaging time and
  the write goes through the panel server, which also rebuilds the page itself;
  for a dropped asset — straight into the dropped `device.json`. If the panel is
  not up, the viewer asks for the file with a dialog once. The "Type schema"
  checkbox labels the parts with callouts, coloured by type;
- that there is a **teardown test**: the "Teardown test" button gives a flat
  top-down view where parts are grabbed with the mouse and pulled apart like a
  jigsaw — a check of the game's disassembly mechanic. The asset does not change
  meanwhile: the offsets live only inside the mode, "Reassemble" or leaving the
  mode puts everything back, a double click puts back one part;
- where each part came from, if the kit was assembled from different pictures;
- **what did not make it in and why** — as a separate line; without it the
  report is incomplete.

If the asset was fixed after packaging through `asset_update` or by hand on
disk — **rebuild the viewer through `asset_viewer`**: in `OPEN_<device>.html`
the asset is baked in as a copy and does not update itself. Edits saved with the
button inside the viewer rebuild the page on their own (the panel server does it
on write).

## The device.json format

```json
{
  "device": "nokia_3310",
  "texture": "texture.png",
  "texture_size": [1279, 1577],
  "canvas": [595, 1336],
  "parts": [
    {
      "id": "back_cover", "name": "Back Cover", "type": "housing_rear",
      "frame": { "x": 3, "y": 847, "w": 362, "h": 730 },
      "corners": [[3,847],[365,847],[365,1577],[3,1577]],
      "uv": [0.002346, 0.537095, 0.285379, 1.0],
      "pivot": [0.5, 0.5],
      "position": [300, 570], "size": [362, 730],
      "scale": 1.0, "rotation": 0.0, "layer": 0
    }
  ]
}
```

`frame` — where to cut from in the texture. `position` — the centre of the part
on the assembly canvas. `layer` — the draw order and the teardown order in the
game. `type` — the shared part type from `PART_TYPES.md` (in the example
`back_cover` has `housing_rear`).

## What not to do

- Do not send the step 2 and step 4 prompts from memory. Only a fresh `Read` of
  the file.
- Do not edit the files in `Prompts/` without an explicit request. Appending a
  block to the text of one retry call is fine and expected — the file does not
  change.
- Do not run `flow_generate` without a reference.
- Do not repeat a call with the same prompt and the same reference.
- Do not take a part you have not looked at with your eyes.
- **Do not take a single volumetric part.** All of them must be flat, no
  exceptions and no "this one is small, it will do".
- Do not nudge one part for more than three iterations — replace it or drop it.
- Do not drag a non-critical part into the assembly if it spoils the look.
- **Do not ship an assembly that falls apart into pieces.** Clamshell halves
  apart, a gap along the joint, a part in the air — that is a defect, not a
  feature.
- **Do not leave the insides visible in the assembled state.** Not a strip of
  board, not a corner of battery, not an edge of membrane. An assembled device
  looks whole.
- **Do not place a part whose shape does not match its seat.**
- **Do not check against the original from memory** — open the user's picture
  again on every assembly round.
- **Do not fit the screen by the part's bounding box** — only by the visible
  matrix.
- **Do not package with the acceptance checklist unclosed**, and do not explain
  an assembly defect in the report instead of fixing it.
- Do not pass a partial result off as finished: a core part is missing — say so
  straight.
