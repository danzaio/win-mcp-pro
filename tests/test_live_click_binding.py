"""Opt-in live fixture for the frame-pinned click (Phase B2).

Run explicitly::

    WINMCP_RUN_LIVE_CLICK=1 python -m pytest tests/test_live_click_binding.py -q

The test creates its own WinForms window, saves and restores the cursor,
and clicks the fixture's button through the frame-pinned path
(zoom -> frame -> click with target=...). The effect is asserted from the
fixture's own state - the window title the button handler sets - not from
the server's reply. Negative cases assert that typed refusals happen
before any input: an unknown frame and a point outside the target.

One note: it moves the real cursor for a moment (restored afterwards) and
requires the fixture window not to be covered; it is opt-in for that
reason. It never targets a window it did not create.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("WINMCP_RUN_LIVE_CLICK") != "1",
    reason="opt-in live click smoke; requires Windows interactive desktop",
)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos() -> tuple[int, int]:
    point = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _cursor_set(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _virtual_origin() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)


def _tool_json(result) -> dict:
    assert result.is_error is False, result.content
    assert result.content and result.content[0].type == "text"
    value = json.loads(result.content[0].text)
    assert isinstance(value, dict)
    return value


def _probe_powershell(title: str, button_name: str, committed_title: str) -> str:
    substitutions = {
        "__TITLE__": title,
        "__BUTTON_NAME__": button_name,
        "__COMMITTED_TITLE__": committed_title,
    }
    template = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$script:probeTitle = '__TITLE__'
$script:probeCommitted = '__COMMITTED_TITLE__'
$script:probeButtonName = '__BUTTON_NAME__'

$script:clickForm = New-Object System.Windows.Forms.Form
$script:clickForm.Text = $script:probeTitle
$script:clickForm.Name = 'McpClickProbeForm'
$script:clickForm.Width = 420
$script:clickForm.Height = 220
$script:clickForm.StartPosition = 'Manual'
$script:clickForm.Location = New-Object System.Drawing.Point(240, 180)
$script:clickForm.TopMost = $true

$script:clickButton = New-Object System.Windows.Forms.Button
$script:clickButton.Name = 'McpClickProbeButton'
$script:clickButton.Text = 'Click me'
$script:clickButton.AccessibleName = $script:probeButtonName
$script:clickButton.SetBounds(120, 90, 180, 44)
$script:clickButton.Add_Click({
    $script:clickForm.Text = $script:probeCommitted
})
$script:clickForm.Controls.Add($script:clickButton)
$script:clickForm.Add_Shown({ $script:clickForm.Activate() })
[void]$script:clickForm.ShowDialog()
"""
    for marker, value in substitutions.items():
        template = template.replace(marker, value.replace("'", "''"))
    return template


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="Windows PowerShell with WinForms is required",
)
def test_frame_pinned_click_hits_only_its_target():
    try:
        # Match the server's per-monitor DPI awareness so both processes
        # read the same physical-pixel space.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    project_root = Path(__file__).resolve().parents[1]
    suffix = uuid.uuid4().hex[:10]
    title = f"WinMCP Click Probe {suffix}"
    committed = title + " | clicked"
    button_name = f"Click Probe Button {suffix}"
    allowed = "list_windows,snapshot,find,get_prop,zoom,click"

    with tempfile.TemporaryDirectory(prefix="win-mcp-click-") as temp_dir:
        script_path = Path(temp_dir) / "click_probe.ps1"
        script_path.write_text(
            _probe_powershell(title, button_name, committed), encoding="utf-8"
        )
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as app_stdout, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as app_stderr, \
                tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as server_stderr:
            app = subprocess.Popen(
                [
                    shutil.which("powershell.exe"),
                    "-NoLogo",
                    "-NoProfile",
                    "-STA",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=app_stdout,
                stderr=app_stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            server_script = textwrap.dedent(
                f"""
                import runpy
                import sys

                sys.argv = ["win_mcp_pro.server.main", "--tools", {allowed!r}]
                runpy.run_module("win_mcp_pro.server.main", run_name="__main__")
                """
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=["-c", server_script],
                cwd=str(project_root),
            )

            def stop_own_child() -> None:
                if app.poll() is not None:
                    return
                app.terminate()
                try:
                    app.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    app.kill()
                    app.wait(timeout=5)
                assert app.poll() is not None, "owned probe process survived cleanup"

            saved_cursor = _cursor_pos()

            async def drive() -> None:
                async with stdio_client(params, errlog=server_stderr) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        assert {tool.name for tool in listed.tools} == {
                            "list_windows", "snapshot", "find", "get_prop",
                            "zoom", "click",
                        }

                        async def call(name: str, args: dict) -> dict:
                            return _tool_json(await session.call_tool(name, args))

                        # 1. wait for the owned fixture window, then bind to it
                        deadline = time.monotonic() + 15
                        target = None
                        while time.monotonic() < deadline:
                            inventory = await call("list_windows", {})
                            target = next(
                                (
                                    window
                                    for window in inventory["windows"]
                                    if window["pid"] == app.pid
                                    and window["title"] == title
                                ),
                                None,
                            )
                            if target is not None:
                                break
                            await asyncio.sleep(0.2)
                        assert target is not None, "the owned click probe did not appear"

                        found = await call("find", {"name": title, "kind": "Window"})
                        assert found["ok"] is True, found
                        window_id = found["element"]["id"]
                        assert window_id.startswith(f"{target['hwnd']}:")
                        window_rect = await call("get_prop", {"id": window_id, "prop": "rect"})
                        assert window_rect["ok"] is True
                        rect = window_rect["value"]

                        found_button = await call(
                            "find", {"name": button_name, "kind": "Button"}
                        )
                        assert found_button["ok"] is True, found_button
                        button_id = found_button["element"]["id"]
                        pid = await call("get_prop", {"id": button_id, "prop": "pid"})
                        assert pid["value"] == app.pid
                        button_rect = await call(
                            "get_prop", {"id": button_id, "prop": "rect"}
                        )
                        assert button_rect["ok"] is True
                        button = button_rect["value"]

                        # 2. zoom exactly the owned window (no user content)
                        vx, vy = _virtual_origin()
                        zoom = await call("zoom", {
                            "left": rect["left"] - vx,
                            "top": rect["top"] - vy,
                            "right": rect["right"] - vx,
                            "bottom": rect["bottom"] - vy,
                        })
                        assert zoom["ok"] is True, zoom
                        frame = zoom["frame"]

                        # button center in the crop's image pixels
                        imgx = (button["left"] + button["right"]) / 2 - frame["left"]
                        imgy = (button["top"] + button["bottom"]) / 2 - frame["top"]

                        # 3. negatives first: typed refusal before any input
                        bad_frame = await call("click", {
                            "x": imgx, "y": imgy, "frame": "f-deadbeef",
                            "target": button_id,
                        })
                        assert bad_frame["error"] == "FRAME_UNKNOWN", bad_frame

                        corner_x = rect["left"] + 4 - frame["left"]
                        corner_y = rect["top"] + 4 - frame["top"]
                        mismatch = await call("click", {
                            "x": corner_x, "y": corner_y, "frame": frame["id"],
                            "target": button_id,
                        })
                        assert mismatch["error"] == "TARGET_MISMATCH", mismatch
                        name_now = await call("get_prop", {"id": window_id, "prop": "name"})
                        assert name_now["value"] == title, "refused click still had an effect"

                        # 4. positive: the frame-pinned click must reach the button
                        hit = await call("click", {
                            "x": imgx, "y": imgy, "frame": frame["id"],
                            "target": button_id,
                        })
                        assert hit["ok"] is True, hit

                        # 5. the effect is asserted from the fixture's own state
                        deadline = time.monotonic() + 5
                        seen = None
                        while time.monotonic() < deadline:
                            seen = await call(
                                "get_prop", {"id": window_id, "prop": "name"}
                            )
                            if seen.get("value") == committed:
                                break
                            await asyncio.sleep(0.1)
                        assert seen["value"] == committed, (
                            "the fixture never recorded the click: %r" % (seen,)
                        )

            try:
                asyncio.run(asyncio.wait_for(drive(), timeout=60))
            finally:
                _cursor_set(*saved_cursor)
                stop_own_child()
