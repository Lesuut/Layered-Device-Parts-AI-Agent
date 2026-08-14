#!/usr/bin/env python3
"""
MCP server over Google Flow: generating 2K pictures from an already open Chrome.

Tools:
  flow_check           - check readiness (debug port, Flow tab, login)
  flow_setup           - bring up debug Chrome if it is not running
  flow_generate        - prompt (+references) -> generation -> WAIT -> download 2K
  flow_paste_reference - only paste a reference into the composer, no generation
  flow_list_files      - show what has already been generated in the output folder

Run as MCP (stdio):
  python mcp_server.py

Registering in Claude Code:
  claude mcp add flow-images -- python "<path>/mcp_server.py"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flow_gen as fg  # noqa: E402
import run as launcher  # noqa: E402

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[assignment]

DEFAULT_OUT = HERE / "output"

mcp = _Server(
    "flow-images",
    instructions=(
        "Image generation in Google Flow (labs.google) through an already open Chrome. "
        "Call flow_check before the first generation; if it is not ready — flow_setup. "
        "flow_generate blocks to the end: it returns control only when every "
        "picture (4 by default) has been generated and downloaded in 2K to disk. "
        "Working from a reference is supported: pass reference_images to flow_generate — "
        "the pictures get pasted into the composer, the upload is waited for, and only "
        "then does generation start."
    ),
)


@mcp.tool()
def flow_check() -> str:
    """Check that the Flow environment is ready.

    Looks at: whether Chrome is running with debug port 9222, whether a Flow project
    tab is open, whether a Google sign-in page is in the way. Call it before flow_generate.
    """
    return json.dumps(fg.check_environment(), ensure_ascii=False, indent=2)


@mcp.tool()
def flow_setup() -> str:
    """Bring up debug Chrome for Flow automation if it is not running yet.

    The user's normal Chrome does not have to be closed — a separate process starts
    with the ChromeDebugProfile profile. If the profile is new, the user has to sign
    in to Google and open the Flow project by hand.
    """
    info = launcher.cdp_version()
    if info:
        return json.dumps(
            {"started": False, "already_running": True, "browser": info.get("Browser")},
            ensure_ascii=False,
        )
    launcher.launch_chrome()
    launcher.wait_for_port(45)
    return json.dumps(
        {
            "started": True,
            "next_step": "If there is no Flow tab — sign in to Google and open the project, "
            "then call flow_check",
        },
        ensure_ascii=False,
    )


@mcp.tool()
def flow_generate(
    prompt: Annotated[str, Field(description="The prompt text for generation")],
    count: Annotated[int, Field(description="How many pictures to wait for", ge=1, le=8)] = 4,
    out_dir: Annotated[
        str | None, Field(description="Where to save; by default the output folder next to the server")
    ] = None,
    quality: Annotated[
        str, Field(description="2k (default, 2400x1792), 1k, 4k (needs a paid plan), src")
    ] = "2k",
    gen_timeout: Annotated[int, Field(description="Waiting for generation, sec")] = 600,
    ui_timeout: Annotated[int, Field(description="Waiting for upscaling and download, sec")] = 180,
    reference_images: Annotated[
        list[str] | None,
        Field(
            description="Reference pictures: absolute paths to files, data: URIs "
            "or http(s) links. They are pasted into the Flow composer (like Ctrl+V) BEFORE "
            "generation; the button is pressed only once they have finished uploading"
        ),
    ] = None,
    ref_method: Annotated[
        str,
        Field(
            description="Paste method: auto (tries each in turn, the default), paste, "
            "clipboard (a genuine Ctrl+V through the Windows clipboard), drop, upload"
        ),
    ] = "auto",
    ref_timeout: Annotated[int, Field(description="Waiting for the reference to upload, sec")] = 90,
) -> str:
    """Generate pictures in Google Flow and download them to disk.

    IMPORTANT: the call blocks and returns ONLY after all `count` pictures have been
    generated and the files saved to disk. The typical time is 1.5-3 minutes for 4
    pictures in 2K. Do not call it again while the previous call has not returned.

    References (`reference_images`) are sample pictures: they get pasted into the Flow
    input field, the script waits for them to finish uploading to the site and only
    then presses "generate". If a reference could not be pasted, generation does NOT
    start and error=reference_upload_failed comes back (no quota is spent).

    Returns JSON: ok, downloaded, folder, elapsed_sec, references and a list of
    images with the fields path, filename, width, height, bytes, upscaled_2k.
    If ok=false — look at the error and message fields.
    """
    result = fg.generate_images(
        prompt=prompt,
        count=count,
        out_dir=out_dir or str(DEFAULT_OUT),
        quality=quality,
        gen_timeout=gen_timeout,
        ui_timeout=ui_timeout,
        references=reference_images,
        ref_method=ref_method,
        ref_timeout=ref_timeout,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def flow_paste_reference(
    reference_images: Annotated[
        list[str],
        Field(description="Paths to files, data: URIs or links to reference pictures"),
    ],
    ref_method: Annotated[
        str, Field(description="auto | paste | clipboard | drop | upload")
    ] = "auto",
    ref_timeout: Annotated[int, Field(description="Waiting for the upload, sec")] = 90,
) -> str:
    """Paste references into the Flow composer WITHOUT generating.

    Useful for checking that pasting works without spending generation quota, or
    for preparing the composer and starting the generation separately.
    Returns JSON with ok and a references list (paste method, errors).
    """
    return json.dumps(
        fg.paste_references(
            references=reference_images, ref_method=ref_method, ref_timeout=ref_timeout
        ),
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def flow_list_files(
    out_dir: Annotated[str | None, Field(description="Output folder")] = None,
    limit: Annotated[int, Field(description="How many recent folders to show", ge=1, le=50)] = 10,
) -> str:
    """Show the most recent generated batches of pictures and the file paths."""
    root = Path(out_dir) if out_dir else DEFAULT_OUT
    if not root.exists():
        return json.dumps({"folder": str(root), "batches": []}, ensure_ascii=False)

    batches = []
    for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
        files = sorted(d.glob("*.*"))
        batches.append(
            {
                "batch": d.name,
                "path": str(d),
                "count": len(files),
                "files": [
                    {
                        "path": str(f),
                        "bytes": f.stat().st_size,
                        "size": (lambda s: f"{s[0]}x{s[1]}" if s else None)(fg.image_size(f)),
                    }
                    for f in files
                ],
            }
        )
    return json.dumps({"folder": str(root), "batches": batches}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
