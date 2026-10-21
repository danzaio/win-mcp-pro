"""Opt-in end-to-end smoke against a disposable WinForms child window.

Run explicitly with ``WINMCP_RUN_LIVE_UIA=1 python -m pytest
tests/test_live_uia.py -q``. The test never targets an existing application,
uses clipboard/input synthesis, writes user files, or kills a process it did
not create.
"""

from __future__ import annotations

import asyncio
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
    sys.platform != "win32" or os.environ.get("WINMCP_RUN_LIVE_UIA") != "1",
    reason="opt-in live UIA smoke; requires Windows interactive desktop",
)


def _tool_json(result) -> dict:
    assert result.is_error is False, result.content
    assert result.content and result.content[0].type == "text"
    value = json.loads(result.content[0].text)
    assert isinstance(value, dict)
    return value


def _process_is_running(pid: int) -> bool:
    """Check the owned MCP child after its stdio transport has shut down."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        state = kernel32.WaitForSingleObject(handle, 0)
        if state == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return state == 0x00000102  # WAIT_TIMEOUT means still running
    finally:
        kernel32.CloseHandle(handle)


def _probe_powershell(
    title: str,
    input_name: str,
    button_name: str,
    status_name: str,
    committed_title: str,
) -> str:
    substitutions = {
        "__PROBE_TITLE__": title,
        "__INPUT_NAME__": input_name,
        "__BUTTON_NAME__": button_name,
        "__STATUS_NAME__": status_name,
        "__COMMITTED_TITLE__": committed_title,
    }
    template = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$script:probeBaseTitle = '__PROBE_TITLE__'
$script:probeCommittedTitle = '__COMMITTED_TITLE__'
$script:probeInputName = '__INPUT_NAME__'
$script:probeButtonName = '__BUTTON_NAME__'
$script:probeStatusName = '__STATUS_NAME__'
$script:probeForm = New-Object System.Windows.Forms.Form
$script:probeForm.Text = $script:probeBaseTitle
$script:probeForm.Name = 'McpProbeForm'
$script:probeForm.Width = 460
$script:probeForm.Height = 190
$script:probeForm.StartPosition = 'CenterScreen'
$script:probeForm.ShowInTaskbar = $true

$script:probeInput = New-Object System.Windows.Forms.TextBox
$script:probeInput.Name = 'McpProbeTextBox'
$script:probeInput.AccessibleName = $script:probeInputName
$script:probeInput.Text = 'initial-value'
$script:probeInput.SetBounds(20, 20, 400, 28)

$script:probeButton = New-Object System.Windows.Forms.Button
$script:probeButton.Name = 'McpProbeCommitButton'
$script:probeButton.Text = 'Commit test value'
$script:probeButton.AccessibleName = $script:probeButtonName
$script:probeButton.SetBounds(20, 60, 180, 32)

$script:probeStatus = New-Object System.Windows.Forms.Label
$script:probeStatus.Name = 'McpProbeStatusLabel'
$script:probeStatus.AccessibleName = $script:probeStatusName
$script:probeStatus.Text = 'Ready'
$script:probeStatus.SetBounds(20, 105, 400, 28)

$script:probeButton.Add_Click({
    $script:probeStatus.Text = 'Committed: ' + $script:probeInput.Text
    $script:probeStatus.AccessibleName = $script:probeStatusName + ' | committed'
    $script:probeForm.Text = $script:probeCommittedTitle
})
$script:probeForm.Controls.Add($script:probeInput)
$script:probeForm.Controls.Add($script:probeButton)
$script:probeForm.Controls.Add($script:probeStatus)
$script:probeForm.Add_Shown({
    $script:probeForm.Activate()
    $script:probeForm.BringToFront()
})
[void]$script:probeForm.ShowDialog()
"""
    for marker, value in substitutions.items():
        template = template.replace(marker, value.replace("'", "''"))
    return template


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="Windows PowerShell with WinForms is required",
)
def test_stdio_uia_vertical_slice_touches_only_its_own_probe_window():
    project_root = Path(__file__).resolve().parents[1]
    suffix = uuid.uuid4().hex[:10]
    title = f"WinMCP UIA Probe {suffix}"
    committed_title = title + " | committed"
    input_name = f"MCP Probe Input {suffix}"
    button_name = f"MCP Probe Commit {suffix}"
    status_name = f"MCP Probe Status {suffix}"
    allowed = "list_windows,snapshot,find,get_prop,set_value,invoke"

    with tempfile.TemporaryDirectory(prefix="win-mcp-pro-live-uia-") as temp_dir:
        server_pid_file = Path(temp_dir) / "server.pid"
        server_exit_file = Path(temp_dir) / "server.exited"
        script_path = Path(temp_dir) / "probe.ps1"
        script_path.write_text(
            _probe_powershell(
                title, input_name, button_name, status_name, committed_title
            ),
            encoding="utf-8",
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
                import atexit
                import os
                import runpy
                import sys
                from pathlib import Path

                server_exit_file = Path({str(server_exit_file)!r})
                Path({str(server_pid_file)!r}).write_text(str(os.getpid()), encoding="ascii")
                atexit.register(lambda: server_exit_file.write_text("exited", encoding="ascii"))
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

            async def drive() -> None:
                async with stdio_client(params, errlog=server_stderr) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listed_tools = await session.list_tools()
                        assert {tool.name for tool in listed_tools.tools} == {
                            "list_windows",
                            "snapshot",
                            "find",
                            "get_prop",
                            "set_value",
                            "invoke",
                        }

                        deadline = time.monotonic() + 15
                        target = None
                        while time.monotonic() < deadline:
                            if app.poll() is not None:
                                app_stderr.seek(0)
                                raise AssertionError(
                                    "probe UI exited before it exposed a window: "
                                    + app_stderr.read()[-2000:]
                                )
                            inventory = _tool_json(
                                await session.call_tool("list_windows", {})
                            )
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
                        assert target is not None, "the owned probe window did not appear"

                        async def resolve_own_root() -> str:
                            # Bind all reads to the owned fixture via full+root:
                            # depth=1 lists every window root without descending,
                            # then the owned HWND selects its runtime root.
                            # No foreground assumption is made here.
                            captured = _tool_json(
                                await session.call_tool(
                                    "snapshot",
                                    {"mode": "full", "depth": 1, "timeout_ms": 5000},
                                )
                            )
                            owned = [
                                element
                                for element in captured["elements"]
                                if element["id"].startswith(f"{target['hwnd']}:")
                            ]
                            assert owned, "owned probe window root not enumerated"
                            for element in owned:
                                if element["kind"] == "Window":
                                    return element["id"]
                            return owned[0]["id"]

                        own_root = await resolve_own_root()

                        async def own_window_snapshot() -> dict:
                            captured = _tool_json(
                                await session.call_tool(
                                    "snapshot",
                                    {
                                        "mode": "full",
                                        "root": own_root,
                                        "depth": 2,
                                        "timeout_ms": 5000,
                                    },
                                )
                            )
                            assert any(
                                window["hwnd"] == target["hwnd"]
                                and window["pid"] == app.pid
                                for window in captured["windows"]
                            ), "owned probe missing from window inventory"
                            assert captured["elements"]
                            assert all(
                                element["id"].startswith(f"{target['hwnd']}:")
                                for element in captured["elements"]
                            ), "snapshot included elements outside the probe HWND"
                            return captured

                        async def prop(element_id: str, name: str):
                            result = _tool_json(
                                await session.call_tool(
                                    "get_prop", {"id": element_id, "prop": name}
                                )
                            )
                            assert result["ok"] is True, result
                            return result["value"]

                        async def find_owned(name: str, kind: str) -> str:
                            result = _tool_json(
                                await session.call_tool(
                                    "find", {"name": name, "kind": kind}
                                )
                            )
                            assert result["ok"] is True, result
                            element_id = result["element"]["id"]
                            assert await prop(element_id, "handle") == target["hwnd"]
                            assert await prop(element_id, "pid") == app.pid
                            return element_id

                        first = await own_window_snapshot()
                        edits = [e for e in first["elements"] if e["kind"] == "Edit"]
                        buttons = [e for e in first["elements"] if e["kind"] == "Button"]
                        assert len(edits) == 1, first["elements"]
                        assert len(buttons) == 1, first["elements"]
                        assert edits[0]["id"].startswith(f"{target['hwnd']}:")
                        assert buttons[0]["id"].startswith(f"{target['hwnd']}:")
                        edit_id = await find_owned(input_name, "Edit")
                        button_id = await find_owned(button_name, "Button")
                        assert await prop(edit_id, "name") == input_name
                        assert await prop(button_id, "name") == button_name
                        assert await prop(edit_id, "is_enabled") is True

                        requested = "mcp-safe-probe-" + uuid.uuid4().hex[:8]
                        write_result = _tool_json(
                            await session.call_tool(
                                "set_value", {"id": edit_id, "text": requested}
                            )
                        )
                        assert write_result["ok"] is True

                        await own_window_snapshot()
                        fresh_edit_id = await find_owned(input_name, "Edit")
                        assert await prop(fresh_edit_id, "value") == requested

                        invoke_result = _tool_json(
                            await session.call_tool("invoke", {"id": button_id})
                        )
                        assert invoke_result["ok"] is True

                        await own_window_snapshot()
                        fresh_status_id = await find_owned(
                            status_name + " | committed", "Text"
                        )
                        assert await prop(fresh_status_id, "name") == (
                            status_name + " | committed"
                        )
                        fresh_inventory = _tool_json(
                            await session.call_tool("list_windows", {})
                        )
                        assert any(
                            window["hwnd"] == target["hwnd"]
                            and window["pid"] == app.pid
                            and window["title"] == committed_title
                            for window in fresh_inventory["windows"]
                        )

            try:
                asyncio.run(asyncio.wait_for(drive(), timeout=60))
            finally:
                stop_own_child()
                if server_pid_file.exists():
                    server_pid = int(server_pid_file.read_text(encoding="ascii"))
                    assert server_exit_file.read_text(encoding="ascii") == "exited", (
                        "stdio server did not exit cleanly"
                    )
                    assert not _process_is_running(server_pid), (
                        "stdio server process survived client shutdown"
                    )

            assert server_pid_file.exists(), "stdio server process did not start"
            server_stderr.seek(0)
            log_lines = server_stderr.read().splitlines()
            events = [json.loads(line) for line in log_lines if '"event"' in line]
            calls = {
                event["tool"]
                for event in events
                if event.get("event") == "tool_call"
            }
            assert {
                "list_windows",
                "snapshot",
                "find",
                "get_prop",
                "set_value",
                "invoke",
            } <= calls
            assert app.poll() is not None, "owned probe process was not cleaned up"
