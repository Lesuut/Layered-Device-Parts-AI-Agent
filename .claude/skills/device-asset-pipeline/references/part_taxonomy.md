# Part kits by device type

The order in the tables is bottom-up, that is `layer` 0 → N. It is also the
teardown order in the game, only reversed: the player removes from the top down.

Mandatory parts are marked in **bold** — without them the asset is incomplete;
say so to the user if such a part turned up on none of the 4 pictures.

## Keypad phone (Nokia-like)

| layer | id | name | How to spot it on the layout |
|---|---|---|---|
| 0 | **back_cover** | Back Cover | a solid back cover, often with a texture/logo |
| 1 | **rear_chassis** | Rear Chassis | tub-shaped body with the battery compartment, ribs and latches visible |
| 2 | **battery** | Battery | rounded rectangle, label, contacts |
| 3 | **main_pcb** | Main Board | green board with chips and a SIM socket |
| 4 | keypad_membrane | Keypad Membrane | light membrane with domes under the keys |
| 5 | **keypad** | Keypad | rubber keypad with digits |
| 6 | **screen** | LCD Screen | rectangular display, often with a ribbon |
| 7 | **front_shell** | Front Shell | front panel with the screen cutout and key holes |

Optional small stuff: `speaker`, `microphone`, `vibro_motor`, `antenna`,
`camera_module`, `flex_cable`, `sim_tray`, `screw`.

## Clamshell phone (flip)

Two halves on a hinge. Assembled **open**, one shared silhouette, the joint
covered by the hinge. Layers are numbered straight through both halves.

| layer | id | name | How to spot it on the layout |
|---|---|---|---|
| 0 | **lid_back_cover** | Lid Back Cover | outer lid of the flip, often with a logo and an external screen |
| 1 | **rear_chassis** | Rear Chassis | tub of the lower half with the battery compartment |
| 2 | **battery** | Battery | rectangle with contacts |
| 3 | **main_pcb** | Main Board | board of the lower half |
| 4 | speaker | Speaker | a barrel or a disc of a speaker |
| 5 | **hinge** | Hinge | the hinge axle: a cylinder or a pair of bushings; ties the halves together |
| 6 | flex_cable | Hinge Flex | a long ribbon running from the lower half into the upper one |
| 7 | lid_chassis | Lid Chassis | frame inside the flip, holds the matrix |
| 8 | **lcd_screen** | LCD Screen | display module with a ribbon |
| 9 | keypad_membrane | Keypad Membrane | backing under the keypad |
| 10 | **keypad** | Keypad | keypad with digits |
| 11 | **front_bezel** | Front Bezel | front bezel of the lower half with the keypad window |
| 12 | **lid_front_shell** | Lid Front Shell | front lid of the flip with the screen window |

`hinge` is mandatory: without it the halves fall apart into two objects in the
assembly. Not in any batch — generate it per step 8.

The widths of the upper and lower halves must match. They do not — bad pick.

## Smartphone

`back_glass` → `mid_frame` → `battery` → `main_pcb` → `shield_plate` →
`camera_module` → `display_module` → `front_glass`

## Player / dictaphone

`back_cover` → `chassis` → `battery` → `main_pcb` → `mechanism` (tape transport
or drive) → `buttons` → `front_shell`

## Handheld console

`back_cover` → `chassis` → `battery` → `main_pcb` → `buttons_pad` (d-pad and
buttons as separate parts, if they can be told apart) → `screen` → `front_shell`

## Remote control

`back_cover` → `battery` (often two) → `contact_membrane` → `main_pcb` →
`buttons` → `front_shell`

## Camera

`back_cover` → `chassis` → `battery` → `main_pcb` → `sensor_module` →
`lens_assembly` → `top_plate` → `front_shell`

## Naming rules

- `id` — Latin letters only, snake_case, unique within the device.
- Several identical parts — a numbered suffix: `screw_1`, `screw_2`.
- `name` — human-readable English, capitalised: `Main Board`.
- Do not invent exotic names: the game looks parts up by stable ids.

## Signs that a part should NOT be taken

- The whole assembled device (it often lands in the layout as a "reference").
- Two parts fused into one silhouette.
- A part shown from the back when there is a front-side variant.
- Fragments of text, arrows, frames, shadows.
- A duplicate of a part already taken, but of worse quality.
