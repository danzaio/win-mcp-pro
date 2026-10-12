"""Regression tests for process exclusions at the snapshot boundary."""

from __future__ import annotations

import asyncio
import json
import types

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from server import main, registry
from tools import snapshot, winproc


class FakeWindow:
    def __init__(self, title: str, pid: int, hwnd: int, *, fail_if_walked=False):
        self.Name = title
        self.ProcessId = pid
        self.NativeWindowHandle = hwnd
        self.ControlTypeName = "WindowControl"
        self.BoundingRectangle = None
        self._fail_if_walked = fail_if_walked
        self.walk_count = 0

    def GetRuntimeId(self):
        return [self.NativeWindowHandle]

    def GetPattern(self, _pattern_id):
        return None

    def GetChildren(self):
        self.walk_count += 1
        if self._fail_if_walked:
            raise AssertionError("excluded process was traversed")
        return []


class FakeLeaf:
    """Child of a walked window: proves traversal happened for non-excluded."""

    def __init__(self, rid: int):
        self.Name = "leaf"
        self.ProcessId = 0
        self.NativeWindowHandle = 0
        self.ControlTypeName = "TextControl"
        self.BoundingRectangle = None
        self._rid = rid

    def GetRuntimeId(self):
        return [self._rid]

    def GetPattern(self, _pattern_id):
        return None

    def GetChildren(self):
        return []


@pytest.fixture
def clear_process_exclusions():
    winproc.clear_exclusions()
    snapshot._EXCLUDED_PROCS.clear()
    yield
    winproc.clear_exclusions()
    snapshot._EXCLUDED_PROCS.clear()


def test_public_exclude_proc_skips_by_pid_executable_before_traversal(
    monkeypatch, clear_process_exclusions
):
    excluded = FakeWindow(
        "Slow application main window", 101, 1001, fail_if_walked=True
    )
    title_lookalike = FakeWindow("slowui.exe", 202, 2002)
    monkeypatch.setattr(snapshot, "_uia", types.SimpleNamespace())
    monkeypatch.setattr(snapshot, "_list_windows", lambda: [excluded, title_lookalike])
    monkeypatch.setattr(
        winproc,
        "_process_snapshot",
        lambda: [
            {"pid": 101, "exe": r"C:\Apps\SlowUI.EXE"},
            {"pid": 202, "exe": "editor.exe"},
        ],
    )

    result = winproc.h_exclude_proc({"names": [r"C:\Apps\slowui.exe"]})
    assert result == {"ok": True, "excluded": ["slowui"]}

    captured = snapshot.snapshot(mode="full", depth=2)

    assert excluded.walk_count == 0
    assert title_lookalike.walk_count == 1
    assert captured.skipped == ["pid:101"]
    assert [window.pid for window in captured.windows] == [202]
    assert [element.name for element in captured.elements] == ["slowui.exe"]


class _RouteWindow(FakeWindow):
    """FakeWindow whose walk yields one leaf, proving non-excluded traversal."""

    def GetChildren(self):
        self.walk_count += 1
        if self._fail_if_walked:
            raise AssertionError("excluded process was traversed")
        return [FakeLeaf(self.NativeWindowHandle + 10000)]


def _drive_mcp_route(monkeypatch, slow, lookalike):
    """Run the public MCP route: exclude_proc -> snapshot via real bridge."""
    monkeypatch.setattr(snapshot, "_uia", types.SimpleNamespace())
    monkeypatch.setattr(snapshot, "_list_windows", lambda: [slow, lookalike])
    monkeypatch.setattr(snapshot, "_active_window_control", lambda: slow)
    monkeypatch.setattr(
        winproc,
        "_process_snapshot",
        lambda: [
            {"pid": 101, "exe": r"C:\Apps\SlowUI.EXE"},
            {"pid": 202, "exe": "editor.exe"},
        ],
    )
    registry.reset_registry()
    registry.register_module(winproc.TOOLS)
    registry.register_module(snapshot.TOOLS)
    server = main.build_server()

    async def run():
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            task = asyncio.create_task(
                server.run(
                    *server_streams,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()

                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        assert result.is_error is False, result.content
                        return json.loads(result.content[0].text)

                    return await _mcp_route_steps(call, slow, lookalike)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return asyncio.run(asyncio.wait_for(run(), timeout=20))


async def _mcp_route_steps(call, slow, lookalike):
    # add: PID/executable match, title lookalike from another exe still walks.
    added = await call("exclude_proc", {"names": [r"C:\Apps\slowui.exe"], "mode": "add"})
    assert added == {"ok": True, "excluded": ["slowui"]}
    first = await call("snapshot", {"mode": "full", "depth": 2})
    assert slow.walk_count == 0
    assert lookalike.walk_count == 1
    assert [w["pid"] for w in first["windows"]] == [202]
    assert first["skipped"] == ["pid:101"]
    assert "leaf" in [e["name"] for e in first["elements"]]

    # explicit root naming an excluded window must not traverse it.
    before = slow.walk_count
    rooted = await call("snapshot", {"mode": "full", "root": "1001:1001", "depth": 2})
    assert slow.walk_count == before, "excluded root was traversed"
    assert rooted["elements"] == []
    assert rooted["skipped"] == ["pid:101"]

    # replace via the public route.
    replaced = await call("exclude_proc", {"names": ["editor.exe"], "mode": "replace"})
    assert replaced == {"ok": True, "excluded": ["editor"]}
    second = await call("snapshot", {"mode": "full", "depth": 2})
    assert [w["pid"] for w in second["windows"]] == [101]
    assert second["skipped"] == ["pid:202"]

    # clear via the public route: both windows walk again.
    cleared = await call("exclude_proc", {"mode": "clear"})
    assert cleared == {"ok": True, "excluded": []}
    third = await call("snapshot", {"mode": "full", "depth": 2})
    assert {w["pid"] for w in third["windows"]} == {101, 202}
    assert third["skipped"] == []

    # all excluded: no fallback walk of the excluded active window.
    both = await call(
        "exclude_proc", {"names": ["slowui.exe", "editor.exe"], "mode": "replace"}
    )
    assert both == {"ok": True, "excluded": ["editor", "slowui"]}
    before = slow.walk_count
    look_before = lookalike.walk_count
    fourth = await call("snapshot", {"mode": "full", "depth": 2})
    assert slow.walk_count == before, "all-excluded fallback traversed excluded active"
    assert lookalike.walk_count == look_before
    assert fourth["elements"] == []
    assert fourth["windows"] == []
    assert set(fourth["skipped"]) == {"pid:101", "pid:202"}


def test_mcp_route_exclusions_cover_root_and_fallback(
    monkeypatch, clear_process_exclusions
):
    slow = _RouteWindow("Slow application main window", 101, 1001, fail_if_walked=True)
    lookalike = _RouteWindow("slowui.exe", 202, 2002)
    try:
        _drive_mcp_route(monkeypatch, slow, lookalike)
    finally:
        registry.reset_registry()
