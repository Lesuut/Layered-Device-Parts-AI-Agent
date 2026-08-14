#!/usr/bin/env python3
"""
MCP server: local background removal, output is PNG with transparency.

Tools:
  bg_check      - what is available: libraries, AI engine, model cache
  bg_remove     - remove the background from files/folder/mask -> PNG with alpha
  bg_inspect    - find unpunched white holes, draw a map with numbers
  bg_punch      - knock out white by the points/numbers the agent picked by eye
  bg_shrink     - cut N px off the whole boundary: remove the white fringe
  bg_install_ai - install rembg+onnxruntime (needed for method=ai)

The order for a clean asset:
  bg_remove -> bg_inspect -> (the agent looks at map_png) -> bg_punch -> bg_shrink

Run as MCP (stdio):
  python mcp_server.py

Registering in Claude Code:
  claude mcp add bg-remover -- python "<path>/mcp_server.py"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bg_remove as br  # noqa: E402
import refine as rf  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]

mcp = _Server(
    "bg-remover",
    instructions=(
        "Local background removal: a picture -> PNG with transparency. "
        "The white engine cuts a white background with no neural net and no internet "
        "(fast, accurate on studio/generated pictures). The ai engine (rembg/U^2-Net) "
        "handles any background but has to be installed. method=auto by default: "
        "white frame edges -> white, otherwise -> ai. The call is synchronous and returns "
        "when the files are already on disk. For a clean asset, bg_remove is followed by "
        "finishing: bg_inspect (a map of unpunched white holes) -> look at the map "
        "with your eyes -> bg_punch by the hole numbers -> bg_shrink (cut the white fringe)."
    ),
)


@mcp.tool()
def bg_check() -> str:
    """Check the background-removal environment.

    Shows the pillow/numpy/scipy versions, whether the AI engine (rembg +
    onnxruntime) is available and which models are already downloaded. The white engine always works.
    """
    return json.dumps(br.check_environment(), ensure_ascii=False, indent=2)


@mcp.tool()
def bg_remove(
    images: Annotated[
        list[str],
        Field(
            description="What to cut: absolute paths to files, a path to a folder "
            "or a mask like C:\\images\\*.jpeg. They can be mixed"
        ),
    ],
    out_dir: Annotated[
        str | None,
        Field(description="Where to put the PNGs. Next to the source by default"),
    ] = None,
    method: Annotated[
        str,
        Field(
            description="auto (default), white — a white background with no neural net, "
            "ai — the rembg neural net for any background"
        ),
    ] = "auto",
    trim: Annotated[
        bool, Field(description="Crop the transparent margins around the object")
    ] = False,
    pad: Annotated[int, Field(description="Padding when trimming, px", ge=0, le=500)] = 0,
    tol_bg: Annotated[
        float,
        Field(
            description="The \"this is background\" threshold, 0..255. Higher cuts "
            "grey/noisy background edges more aggressively",
            ge=0,
            le=128,
        ),
    ] = 12.0,
    tol_fg: Annotated[
        float,
        Field(description="Full-opacity threshold, 0..255", ge=1, le=255),
    ] = 45.0,
    edge: Annotated[
        int, Field(description="Width of the soft edge, px", ge=0, le=20)
    ] = 2,
    feather: Annotated[
        float, Field(description="Alpha blur, sigma. 0 — a hard edge", ge=0, le=10)
    ] = 0.0,
    keep_holes: Annotated[
        bool,
        Field(
            description="Keep white areas not connected to the frame border "
            "(white parts of the object, highlights). Switch it off if you need see-through holes"
        ),
    ] = True,
    shrink: Annotated[
        int,
        Field(
            description="Cut N px inward along every boundary with transparency — "
            "removes the thin white outline left by an imprecise cutout. 2 is normal "
            "for asset parts. 0 — leave the edge alone",
            ge=0,
            le=20,
        ),
    ] = 0,
    ai_model: Annotated[
        str, Field(description="For method=ai: general, u2net, human, anime, fast")
    ] = "general",
    alpha_matting: Annotated[
        bool, Field(description="For method=ai: a soft edge, noticeably slower")
    ] = False,
    suffix: Annotated[str, Field(description="Suffix of the output file name")] = "_nobg",
    recursive: Annotated[
        bool, Field(description="Walk nested folders")
    ] = False,
) -> str:
    """Remove the background and save a PNG with transparency.

    A synchronous call: it returns when every file has been written to disk.
    The white engine runs at roughly 1.5-2 s per 2400x1792 picture;
    the ai engine on CPU — 5-15 s per picture.

    Returns JSON: ok, total, done, failed and a results list, each holding
    path (the finished PNG), method, width/height, bytes, opaque_share (what share
    of the frame the object took), notes with warnings.

    If opaque_share is close to 1, the background was not found (raise tol_bg or
    switch to method="ai"). If it is close to 0, almost everything was cut away.

    Files with the `suffix` are skipped so we do not cut our own output.
    """
    result = br.remove_background_batch(
        images,
        out_dir=out_dir,
        recursive=recursive,
        method=method,
        tol_bg=tol_bg,
        tol_fg=tol_fg,
        edge=edge,
        feather=feather,
        trim=trim,
        pad=pad,
        keep_holes=keep_holes,
        shrink=shrink,
        ai_model=ai_model,
        alpha_matting=alpha_matting,
        suffix=suffix,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def bg_inspect(
    image: Annotated[
        str, Field(description="Path to an already cut PNG with transparency (after bg_remove)")
    ],
    out_dir: Annotated[
        str | None, Field(description="Where to put the map. Next to the picture by default")
    ] = None,
    tol: Annotated[
        float,
        Field(
            description="What counts as white, 0..255. 30 is normal. Raise it to 45 "
            "if the holes are greyish; lower it to 18 if a whole light part gets caught",
            ge=0,
            le=128,
        ),
    ] = 30.0,
    min_area: Annotated[
        int, Field(description="Do not show blobs smaller than N px", ge=1, le=100000)
    ] = 40,
    max_regions: Annotated[
        int, Field(description="How many blobs to mark at most", ge=1, le=300)
    ] = 60,
) -> str:
    """Find white that stayed OPAQUE and draw a map with numbers.

    Why: bg_remove with keep_holes deliberately does not knock out white inside the
    object, otherwise it would eat the white keys and the silkscreen on the board.
    Because of that, genuine see-through holes (the screen window, holes in the board,
    gaps between the shell ribs) stay filled with white. Only the eye can tell a hole
    from a white part — this tool prepares the material for that check.

    Returns JSON: map_png (THE PICTURE HAS TO BE OPENED AND LOOKED AT — the object on
    a dark checker, every white blob outlined and numbered), map_json,
    count and a regions list. Every region has: id, seed [x,y] (a point inside the
    blob, in the pixels of the original), bbox, area_px, whiteness, enclosed
    (the blob is surrounded by the body of the object on every side — the main sign of a hole),
    touches_frame.

    Then: you read the picture, decide which numbers are real holes,
    and pass them to bg_punch(ids=[...]). You do not touch white PARTS.
    """
    return json.dumps(
        rf.inspect_image(
            image, out_dir=out_dir, tol=tol, min_area=min_area, max_regions=max_regions
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def bg_punch(
    image: Annotated[str, Field(description="The same PNG you fed to bg_inspect")],
    ids: Annotated[
        list[int] | None,
        Field(description="Numbers of blobs from the bg_inspect map that have to be knocked out"),
    ] = None,
    points: Annotated[
        list[str] | None,
        Field(
            description="Your own points in the pixels of the ORIGINAL, like \"640x320\" — for holes "
            "bg_inspect did not find (small or greyish ones)"
        ),
    ] = None,
    map_json: Annotated[
        str | None,
        Field(description="Path to <name>_holes.json. If omitted, it is looked for next to the picture"),
    ] = None,
    out: Annotated[
        str | None, Field(description="Where to save. Overwrites the source by default")
    ] = None,
    tol: Annotated[
        float, Field(description="The same white threshold as in bg_inspect", ge=0, le=128)
    ] = 30.0,
    edge: Annotated[
        int, Field(description="Soft edge along the hole border, px", ge=0, le=20)
    ] = 2,
    mode: Annotated[
        str,
        Field(
            description="flood — fill the connected white area under the point (the usual case). "
            "ball — wipe white inside a circle of radius around the point, if the fill leaks outward"
        ),
    ] = "flood",
    radius: Annotated[
        int, Field(description="Radius for mode=ball, px", ge=1, le=2000)
    ] = 24,
    shrink: Annotated[
        int, Field(description="Also cut N px off the boundary (bg_shrink can do it separately)", ge=0, le=20)
    ] = 0,
) -> str:
    """Knock out the white holes at the points the agent picked by eye off the map.

    Every point/id is a seed: from it the whole connected white area is flood-filled
    and its alpha goes to 0, with a soft edge along the border.

    Returns JSON: punched (how many points worked), missed, punched_px,
    opaque_before/opaque_after and, per point, whether it worked and why not
    ("not white under the point, or already transparent").

    If more disappeared than intended, the area leaked outward through a gap in the
    contour: call it again with mode="ball" and a small radius, or lower tol.
    """
    return json.dumps(
        rf.punch_image(
            image,
            points=points,
            ids=ids,
            map_path=map_json,
            out=out,
            tol=tol,
            edge=edge,
            mode=mode,
            radius=radius,
            shrink=shrink,
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def bg_shrink(
    images: Annotated[
        list[str],
        Field(description="PNGs with transparency: paths, a folder or a mask. They can be mixed"),
    ],
    px: Annotated[
        int,
        Field(
            description="How many pixels to cut inward along the whole boundary. 2 is normal",
            ge=0,
            le=20,
        ),
    ] = 2,
    out_dir: Annotated[
        str | None, Field(description="Where to put them. Overwrites the sources by default")
    ] = None,
    soft: Annotated[
        float, Field(description="Smoothing of the new edge, sigma. 0 — a hard edge", ge=0, le=5)
    ] = 0.5,
    min_island: Annotated[
        int, Field(description="Throw away leftover pieces smaller than N px", ge=0, le=10000)
    ] = 6,
    recursive: Annotated[bool, Field(description="Walk nested folders")] = False,
) -> str:
    """Cut px pixels inward everywhere opaque touches transparent.

    Why: after any cutout a thin light outline stays along the contour — a boundary
    pixel is half background and its colour cannot be recovered any more.
    It is easier to throw those pixels away. The erosion runs along the outer contour
    and around the punched holes alike. Pixels butting against the frame border are not cut.

    Do it as the LAST step — after bg_punch, otherwise a new hole brings its own
    fringe back.

    Returns JSON per file: removed_px, removed_share and a warning if more than a
    third of the part was cut away (meaning px is too large for a thin part).
    """
    return json.dumps(
        rf.shrink_batch(
            images,
            px=px,
            out_dir=out_dir,
            recursive=recursive,
            soft=soft,
            min_island=min_island,
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def bg_install_ai(
    gpu: Annotated[
        bool,
        Field(description="Install onnxruntime-gpu instead of the CPU version (needs CUDA)"),
    ] = False,
) -> str:
    """Install the AI engine: pip install rembg onnxruntime (~100 MB of wheels).

    Needed only for method="ai" (a non-white background). Ask the user before
    calling it — this installs packages into their Python. The model (~180 MB) is
    downloaded separately on the first method="ai" run.
    """
    return json.dumps(br.install_ai(gpu=gpu), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
