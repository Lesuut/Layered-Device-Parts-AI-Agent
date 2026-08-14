#!/usr/bin/env python3
"""
MCP server for the 2D device asset builder.

Tools:
  asset_extract  - cut a transparent PNG into parts + a contact sheet
  asset_punch    - punch white inner areas through to transparent (cutouts)
  asset_pack     - pack the chosen parts into an atlas + device.json
  asset_render   - render the assembly (flat/exploded/grid) for a visual check
  asset_update   - move/scale/rotate parts in device.json
  asset_validate - check device.json for duplicates, strays, overlapping frames
  asset_package  - put the finished asset in a folder together with viewer.html

Run as MCP (stdio):
  python mcp_server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import asset_builder as ab  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]

mcp = _Server(
    "asset-builder",
    instructions=(
        "Building a 2D device asset for a take-apart game. The pipeline: "
        "asset_extract (cut the parts and look at the contact sheet) -> pick the "
        "ones you need by eye -> asset_punch (punch the cutouts in the shells) -> "
        "asset_pack (atlas + device.json) -> asset_render + asset_update in a loop "
        "until the assembly looks like a whole device -> asset_validate -> asset_package. "
        "Every tool is synchronous, the files are on disk by the time it returns."
    ),
)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
def asset_extract(
    images: Annotated[
        list[str],
        Field(description="PNG with the background ALREADY removed (after bg_remove): paths, folder or mask"),
    ],
    out_dir: Annotated[str, Field(description="Where to put the extracted parts")],
    alpha_thr: Annotated[int, Field(description="Alpha threshold, 0-255", ge=1, le=254)] = 40,
    min_area_frac: Annotated[
        float, Field(description="Min. part area as a share of the frame", ge=0.0, le=0.5)
    ] = 0.0004,
    min_side: Annotated[int, Field(description="Min. part side, px", ge=2, le=500)] = 14,
    close_gaps: Annotated[
        int, Field(description="Gap bridging, px: saves thin ribbon cables", ge=0, le=10)
    ] = 2,
) -> str:
    """Cut a picture of parts into separate PNGs and build a contact sheet.

    A part = a connected area of opaque pixels. Returns the list of parts with
    indices and sizes plus the path to contact_sheet.png — you MUST read the
    contact sheet with your eyes (Read); every part on it is labelled #N.
    Use those numbers to choose what goes into the asset and what is rubbish
    (fragments of text, shadows, duplicates, pieces of a frame).

    If suspiciously few parts came out, the background is most likely still
    there: run bg_remove with method="white" first.
    """
    return _dump(
        ab.extract_parts(images, out_dir, alpha_thr, min_area_frac, min_side,
                         close_gaps=close_gaps)
    )


@mcp.tool()
def asset_punch(
    files: Annotated[list[str], Field(description="PNGs of parts that need see-through holes")],
    tol: Annotated[
        float, Field(description="White threshold, 0-255. Higher is more aggressive", ge=0, le=200)
    ] = 42.0,
    min_hole_px: Annotated[int, Field(description="Min. hole area, px", ge=1)] = 24,
    suffix: Annotated[
        str, Field(description="Suffix of the new file. Empty = overwrite in place")
    ] = "",
) -> str:
    """Punch the cutouts: white INSIDE a part becomes transparent.

    After the background is removed, the screen cutout, the key holes and the
    screw holes stay white (keep_holes preserves them on purpose). In a game
    asset the layer below has to show through them — this tool punches them out.

    Apply it SELECTIVELY, only to shells, bezels and front panels.
    Do NOT apply it to light parts (white keypad membrane, battery label,
    silvery screens) — there the light area is the part itself.
    After the call, look at the part with your eyes: check nothing extra was
    eaten (the punched_share field tells you what share of the area went).
    """
    return _dump(ab.punch_holes(files, tol, min_hole_px, suffix=suffix))


@mcp.tool()
def asset_pack(
    parts: Annotated[
        list[dict],
        Field(
            description='Parts in layer order, bottom-up. Each one: '
            '{"file":"...png","id":"back_cover","name":"Back Cover",'
            '"type":"housing_rear","layer":0,'
            '"position":[x,y],"size":[w,h],"rotation":0}. Only file is required; '
            'layer defaults to the position in the list, position = canvas centre, '
            'size = the natural size of the part, type = misc. '
            'type is the shared part classification, taken from PART_TYPES.md '
            'in the project root (read the file, find a fitting type, '
            'and if there is none, add a new row to the table first)'
        ),
    ],
    out_dir: Annotated[str, Field(description="Folder for texture.png and device.json")],
    device: Annotated[str, Field(description="Device identifier, e.g. nokia_3310")] = "device",
    padding: Annotated[int, Field(description="Gap between parts in the atlas, px", ge=0, le=64)] = 6,
    canvas: Annotated[
        list[int] | None,
        Field(description="Assembly canvas [W,H]. Without it, computed from the largest part"),
    ] = None,
) -> str:
    """Pack the chosen parts into one texture and create device.json.

    Frames in the atlas are guaranteed NOT to overlap (shelf packing with a gap).
    In device.json every part gets: frame (x,y,w,h), corners (4 corners), uv,
    position (centre on the canvas), size, scale, rotation, layer.

    The layer order is the assembly order bottom-up: 0 is the lowest part
    (usually the back cover), the last one is the top (the front panel).
    """
    return _dump(ab.pack_atlas(parts, out_dir, device, padding, canvas=canvas))


@mcp.tool()
def asset_render(
    json_path: Annotated[str, Field(description="Path to device.json")],
    mode: Annotated[
        str,
        Field(description="flat — the assembled device; exploded — layers spread apart; "
              "grid — every part separately with a label"),
    ] = "flat",
    out: Annotated[str | None, Field(description="Where to save the PNG preview")] = None,
    background: Annotated[
        str, Field(description="checker | white | black | transparent")
    ] = "checker",
    labels: Annotated[bool, Field(description="Label the parts right on the preview")] = False,
) -> str:
    """Render the assembly into a PNG so it can be checked by eye.

    You MUST open the resulting preview through Read and look at it:
    - no part sticks out of the shell, hangs off to the side or covers too much;
    - the keys land in their holes, the screen in its cutout, the battery in its bay;
    - the layer order is right (lower parts are not on top of upper ones).
    Whatever is wrong, fix it through asset_update and render again. It usually
    takes 2-4 iterations.
    """
    return _dump(ab.render_assembly(json_path, out, mode, background, labels))


@mcp.tool()
def asset_update(
    json_path: Annotated[str, Field(description="Path to device.json")],
    updates: Annotated[
        list[dict],
        Field(
            description='Edits by id: [{"id":"battery","dx":0,"dy":-12}] or '
            '{"id":"keypad","position":[300,720],"size":[305,392],"rotation":0,'
            '"layer":5,"scale":1.0,"name":"Keypad","type":"button_pad"}. '
            'dx/dy — offset relative to the current position, position — absolute '
            'centre, type — the classification from PART_TYPES.md'
        ),
    ],
) -> str:
    """Move, scale, rotate parts or change their layer.

    Pass only the fields you are changing. After the edits you must call
    asset_render and look at the result with your eyes.
    """
    return _dump(ab.update_parts(json_path, updates))


@mcp.tool()
def asset_validate(
    json_path: Annotated[str, Field(description="Path to device.json")],
) -> str:
    """Check device.json: duplicate ids, several parts on one layer,
    frames outside the texture, overlapping frames in the atlas, parts far off canvas.

    Call it before asset_package. Overlapping frames are critical: the game cuts
    by coordinates and would grab a neighbouring part.
    """
    return _dump(ab.validate_device(json_path))


@mcp.tool()
def asset_viewer(
    json_path: Annotated[str, Field(description="Path to device.json")],
    out_html: Annotated[
        str | None, Field(description="Where to save the html; next to device.json by default")
    ] = None,
) -> str:
    """Build an HTML file with the asset BAKED IN: double click and the assembly is on screen.

    A page on file:// cannot pull in the device.json and texture.png next to it
    (the browser blocks local requests), so both the JSON and the texture are put
    inside the html itself in base64. The file is self-contained: it can be sent
    on or opened on another machine.

    `asset_package` makes such a file automatically — this tool on its own is
    for when the asset was fixed and only the viewer has to be rebuilt.
    """
    return _dump(ab.build_standalone_viewer(json_path, out_html or ""))


@mcp.tool()
def asset_package(
    json_path: Annotated[str, Field(description="Path to device.json")],
    out_dir: Annotated[str, Field(description="Folder of the finished asset")],
    with_viewer: Annotated[bool, Field(description="Put viewer.html next to it")] = True,
) -> str:
    """Assemble the finished asset: texture.png + device.json + preview + viewer.html.

    This is the final step. The response holds paths to every file; show them to
    the user and say that viewer.html opens on a double click and the asset
    folder (or device.json + texture.png) has to be dragged into it.
    """
    return _dump(ab.package_asset(json_path, out_dir, with_viewer))


if __name__ == "__main__":
    mcp.run()
