# Parts Library

A warehouse of every piece `asset_extract` has ever produced — the ones that
went into an atlas and the ones that were thrown away. The point is simple: a
missing part has often already been cut from an earlier device, so there is no
need to spend a Flow generation on it.

## What is where

```
used/*.png                  parts that went into finished assets
unused/*.png                everything else — cut and sitting idle
index.json                  registry: sha1 -> device, size, origin, used, id, type
_contact_used_NN.png        contact sheets for the eye, 96 parts per page
_contact_unused_NN.png
library.py                  the warehouse script
```

There is no per-device split: everything sits in one pile so it can be flipped
through at once. Where a part came from is visible in its file name, which
starts with the device: `discman_part_003_2b1f2fe1.png`. The tail is the first
8 characters of the sha1, which is also the dedup key — cutting the same image
twice does not grow the warehouse, the record just collects another entry in
`origins`.

On a contact sheet the label is green for used parts, grey for free ones.

## Maintained by itself

A `PostToolUse` hook in `.claude/settings.json`:

- after `asset_extract` — pulls the whole cut folder into `unused/`;
- after `asset_pack` — moves whatever went into the atlas to `used/`, writes
  their `id` and `type`, and redraws both contact sheets.

Nothing needs copying by hand.

## Commands

```bash
py "Parts Library/library.py" stats              # what is in the warehouse
py "Parts Library/library.py" scan               # walk work/ and assets/ again
py "Parts Library/library.py" rebuild            # clear the marks and sort again
py "Parts Library/library.py" ingest <dir>       # pull in one folder of cut parts
py "Parts Library/library.py" sheet              # redraw the contact sheets
py "Parts Library/library.py" mark <device.json> # mark a finished asset's parts
```

`scan` is safe: running it again duplicates nothing. `rebuild` is for when the
marks have drifted — for instance after a part was pulled out of an asset by
hand.

## Marking "used" after the fact

A finished `device.json` carries no source file names, so `mark` matches parts
by `frame.w/h` — those equal the natural size of the cut PNG. For new builds
the marking is exact: the hook sets it from the paths in the `asset_pack` call.
