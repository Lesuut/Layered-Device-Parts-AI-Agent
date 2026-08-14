# Part types

A shared classification: every part in `device.json` has a `type` field. The
type answers "what kind of thing is this at all", not "which device is it
from". A Tamagotchi screen, a Nokia display and a console matrix are all three
`display`.

Why: the game treats parts of the same type the same way (repairs them, paints
them, highlights them), and in the viewer the type colours the part on the
schema — one type is always one colour, because the colour is derived from the
hash of the name.

## How to use this (rule for Claude)

1. Before assigning a type to a part — **read this file**.
2. Look for a fitting type among the ones already written down. If it fits in
   meaning, take it, even if the part is named differently: `lcd_screen`,
   `display_module` and `screen_glass`+matrix — all of it is `display`.
3. Nothing fits — **add a new type to the table**: a row with the `type`, a
   description and example ids. Only then use it in `device.json`.
4. A type is written in Latin letters, singular, snake_case: `display`,
   `circuit_board`, `battery`. Do not breed synonyms: `pcb` and `board` are one
   type, `circuit_board`.
5. A type is mandatory for every part. A completely unclear part is `misc`.

The rule about synonyms matters more than the rest: two names for one thing
give two different colours on the schema and break the whole point of the
classification.

## Table of types

| type | What it is | Example ids |
|---|---|---|
| `housing_front` | Front of the shell, with cutouts for the screen and buttons | `front_shell`, `keypad_bezel` |
| `housing_rear` | Back cover and tub-shaped shell, inner side | `back_cover`, `rear_chassis` |
| `housing_part` | Other pieces of the shell: compartment lid, overlay, blanking plug | `top_cap`, `battery_door` |
| `display` | The screen as a whole: matrix, display module, protective glass | `lcd_screen`, `display_module`, `screen_glass` |
| `circuit_board` | Printed circuit board with components | `main_pcb`, `sub_board` |
| `chassis` | Load-bearing frame inside the shell: metal chassis, mid frame | `chassis`, `mid_frame` |
| `sensor` | Light-sensitive matrix, image sensor | `sensor_module`, `image_sensor` |
| `battery` | Power source: cell, coin battery, rechargeable pack | `battery` |
| `contact` | Metal contact, spring, power terminal | `battery_contact`, `spring` |
| `button` | A single pressable button or key | `button_a`, `btn_cross`, `btn_home` |
| `button_pad` | Keypad or d-pad as one part | `keypad`, `dpad` |
| `membrane` | Conductive membrane under the buttons | `keypad_membrane`, `membrane_dpad` |
| `stick` | Analogue stick, joystick | `stick_left`, `stick_right` |
| `trigger` | Trigger, bumper, side key | `trigger_l1`, `trigger_r2` |
| `speaker` | Speaker, buzzer, beeper | `buzzer`, `speaker` |
| `antenna` | Antenna, coil, whip | `antenna`, `coil` |
| `cable` | Ribbon cable, wire, flex board | `flex_cable` |
| `fastener` | Fasteners: screw, bolt, latch | `screw_1`, `screw_2` |
| `optics` | Lens, objective, LED, flash | `lens`, `led` |
| `mechanism` | Moving assembly: drive, tape transport, gears | `tape_mechanism` |
| `media` | Removable media: disc, cassette, cartridge, memory card | `cd_disc`, `cassette`, `cartridge` |
| `misc` | Nothing else fit | — |

## Who writes this

`type` gets into `device.json` at the atlas packing step (`asset_pack`) —
Claude passes it along with `id` and `name`. It can be changed afterwards in
two ways: `asset_update` with a `type` field, or straight in the viewer — every
row in the parts panel has a type field, and "Save" hands back a corrected
`device.json`.
