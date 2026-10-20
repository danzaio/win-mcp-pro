"""Task 9 - window/process RE tools (win_mcp_pro/tools/winproc.py).

Contract tests for the winproc module. Real Win32 calls are exercised for the
cheap, side-effect-free probes (list_windows); anything destructive is only ever tested against monkeypatched seams, never executed for real.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pydantic
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from win_mcp_pro.tools import winproc as wp  # noqa: E402


def json_schema(tool):
    """House style keeps ``schema`` as a pydantic model class; export it."""
    schema = tool["schema"]
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return schema

REQUIRED_TOOLS = [
    "list_windows",
    "app_launch",
    "win_move",
    "win_resize",
    "win_min",
    "win_max",
    "win_restore",
    "win_snap",
    "exclude_proc",
]


def call(tool_name, **kwargs):
    """Invoke a tool by name through the TOOLS registry."""
    for tool in wp.TOOLS:
        if tool["name"] == tool_name:
            return tool["handler"](kwargs)
    raise AssertionError(f"tool not registered: {tool_name}")


@pytest.fixture(autouse=True)
def clean_exclusions():
    wp.clear_exclusions()
    yield
    wp.clear_exclusions()


# --------------------------------------------------------------------------
# registry contract
# --------------------------------------------------------------------------

def test_tools_contract():
    assert wp.TOOLS, "TOOLS must not be empty"
    names = [t["name"] for t in wp.TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names"
    for tool in wp.TOOLS:
        assert set(tool) >= {"name", "description", "schema", "handler"}
        assert callable(tool["handler"])
        assert tool["description"]
        assert isinstance(tool["schema"], type)
        assert issubclass(tool["schema"], pydantic.BaseModel)
        schema = json_schema(tool)
        assert schema["type"] == "object"
        assert "properties" in schema
        for field in schema.get("required", []):
            assert field in schema["properties"]


def test_all_task_tools_registered():
    names = {t["name"] for t in wp.TOOLS}
    missing = [n for n in REQUIRED_TOOLS if n not in names]
    assert not missing, f"missing tools: {missing}"


def test_no_tool_touches_stdout(capsys):
    call("exclude_proc", names=["nope.exe"])
    captured = capsys.readouterr()
    assert captured.out == ""


# --------------------------------------------------------------------------
# list_windows
# --------------------------------------------------------------------------

def test_list_windows_non_empty():
    res = call("list_windows")
    assert res["count"] == len(res["windows"])
    if not res["windows"]:
        pytest.skip("no visible top-level window on this desktop")
    for w in res["windows"]:
        assert isinstance(w["hwnd"], int) and w["hwnd"] > 0
        assert isinstance(w["title"], str)
        assert isinstance(w["pid"], int)
        assert isinstance(w["visible"], bool)
        assert w["title"], "visible windows should carry a title"


# --------------------------------------------------------------------------
# exclusion set (consulted by snapshot / traversal)
# --------------------------------------------------------------------------

def test_exclude_proc_add_and_is_excluded():
    call("exclude_proc", names=["Chrome.EXE", "foo.exe"])
    assert wp.is_excluded("chrome.exe")
    assert wp.is_excluded("C:\\Program Files\\Google\\Chrome\\CHROME.EXE")
    assert not wp.is_excluded("notepad.exe")
    assert not wp.is_excluded(None)


def test_exclude_proc_replace_and_clear():
    call("exclude_proc", names=["a.exe"])
    call("exclude_proc", mode="replace", names=["b.exe"])
    assert not wp.is_excluded("a.exe")
    assert wp.is_excluded("b.exe")
    call("exclude_proc", mode="clear")
    assert not wp.is_excluded("b.exe")


def test_snapshot_contract_respects_exclusion_set():
    """Contract with a fake snapshot: traversal must never see excluded exes."""
    fake = [
        {"exe": "chrome.exe", "hwnd": 1},
        {"exe": "notepad.exe", "hwnd": 2},
        {"exe": None, "hwnd": 3},
    ]
    call("exclude_proc", names=["chrome"])
    kept = wp.filter_excluded(fake)
    assert [w["hwnd"] for w in kept] == [2, 3]
    assert wp.filter_excluded([]) == []


# --------------------------------------------------------------------------
# window geometry (validated, no accidental clicks)
# --------------------------------------------------------------------------

def test_snap_rect_math_halves_the_work_area():
    work = (0, 0, 1920, 1040)
    assert wp._snap_rect(work, "left") == (0, 0, 960, 1040)
    assert wp._snap_rect(work, "right") == (960, 0, 960, 1040)
    assert wp._snap_rect(work, "top") == (0, 0, 1920, 520)
    assert wp._snap_rect(work, "bottom") == (0, 520, 1920, 520)
    assert wp._snap_rect(work, "topleft") == (0, 0, 960, 520)
    assert wp._snap_rect(work, "bottomright") == (960, 520, 960, 520)


def test_win_resize_rejects_degenerate_size():
    with pytest.raises(pydantic.ValidationError):
        call("win_resize", hwnd=1, width=0, height=100)


def test_win_snap_rejects_unknown_side():
    with pytest.raises(pydantic.ValidationError):
        call("win_snap", hwnd=1, side="diagonal")


def test_win_move_needs_a_real_window():
    with pytest.raises(wp.WinProcError) as ei:
        call("win_move", hwnd=1, x=10, y=10)
    assert ei.value.code == "NOT_FOUND"


# --------------------------------------------------------------------------
# app_launch
# --------------------------------------------------------------------------

def test_app_launch_needs_exactly_one_target():
    with pytest.raises(pydantic.ValidationError):
        call("app_launch")
    with pytest.raises(pydantic.ValidationError):
        call("app_launch", app="notepad", path="C:\\Windows\\System32\\notepad.exe")


def test_app_launch_runs_resolved_path(monkeypatch):
    seen = {}

    def fake_resolve(target, cwd=None):
        seen["resolve"] = target
        return "C:\\fake\\target.exe"

    def fake_create(path, cmdline, cwd, show_cmd):
        seen["create"] = (path, cmdline, cwd)
        return {"pid": 4242, "path": path}

    monkeypatch.setattr(wp, "_resolve_app_path", fake_resolve)
    monkeypatch.setattr(wp, "_create_process", fake_create)
    monkeypatch.setattr(wp, "_wait_for_window", lambda pid, timeout_ms, title=None: 777)

    res = call("app_launch", path="C:\\fake\\target.exe", argv=["a", "b c"], cwd="C:\\tmp")
    assert res["ok"] is True
    assert res["pid"] == 4242
    assert res["hwnd"] == 777
    assert seen["create"][0] == "C:\\fake\\target.exe"
    assert "b c" in seen["create"][1]


def test_app_launch_falls_back_to_shell_when_unresolved(monkeypatch):
    launched = {}

    monkeypatch.setattr(wp, "_resolve_app_path", lambda target, cwd=None: None)
    monkeypatch.setattr(
        wp, "_shell_execute", lambda target, args, cwd, show: launched.update({"target": target}) or 42
    )
    monkeypatch.setattr(wp, "_wait_for_window", lambda pid, timeout_ms, title=None: None)

    res = call("app_launch", name="calc", argv=[])
    assert res["ok"] is True
    assert launched["target"] == "calc"


def test_app_launch_reports_failed_resolution(monkeypatch):
    monkeypatch.setattr(wp, "_resolve_app_path", lambda target, cwd=None: None)
    monkeypatch.setattr(wp, "_shell_execute", lambda target, args, cwd, show: 2)  # SE_ERR
    with pytest.raises(wp.WinProcError) as ei:
        call("app_launch", name="does-not-exist-xyz")
    assert ei.value.code == "LAUNCH_FAILED"


# --------------------------------------------------------------------------
# kill_proc: confirm-first, protected targets, name matching
# --------------------------------------------------------------------------


