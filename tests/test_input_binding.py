"""Input binding (Phase B2): actions bind to the element they intend.

Safe profile (default): an action with coordinates requires the frame id of
a fresh capture plus the target element id; the translated desktop point
must land inside the target's rectangle and hit-test (and integrity-check)
to the target's process before a single input event is sent. Keyboard input
requires the target too, and the server activates it (with a deadline) or
refuses. Any refusal returns a typed error and sends zero input events -
the spy in these tests proves it. ``--input-unrestricted`` restores the
legacy global input for callers that need it; provided bindings are still
verified.

Everything here runs against fakes: ``_send_input`` is a spy, so no event
ever reaches the machine.
"""

from __future__ import annotations

import pytest

from win_mcp_pro.server import main as server_main, registry
from win_mcp_pro.tools import compat, vision


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeControl:
    """Stand-in for a uiautomation control with identity + geometry."""

    def __init__(self, pid=100, rect=(100, 40, 200, 140)):
        self.ProcessId = pid
        self.BoundingRectangle = FakeRect(*rect)


def _handler(name):
    return next(t["handler"] for t in compat.TOOLS if t["name"] == name)


def _register_frame(left=0.0, top=0.0, scale=1.0, width=80, height=60):
    return vision._frame_register(monitor=0, left=left, top=top, scale=scale,
                                  width=width, height=height)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("WINMCP_INPUT_UNRESTRICTED", raising=False)
    if hasattr(compat, "set_unrestricted"):
        compat.set_unrestricted(False)
    if hasattr(vision, "_frames"):
        vision._frames.clear()
    yield
    if hasattr(compat, "set_unrestricted"):
        compat.set_unrestricted(False)
    if hasattr(vision, "_frames"):
        vision._frames.clear()


@pytest.fixture()
def spy(monkeypatch):
    calls: list[list[dict]] = []
    monkeypatch.setattr(compat, "_send_input",
                        lambda events: calls.append([dict(e) for e in events]))
    return calls


@pytest.fixture()
def bound(monkeypatch):
    """A resolvable target plus the standard OS seams (all passing)."""
    control = FakeControl(pid=100, rect=(100, 40, 200, 140))
    monkeypatch.setattr(compat, "_resolve_control",
                        lambda element_id: control if element_id == "t1" else None,
                        raising=False)
    monkeypatch.setattr(compat, "_hit_test_pid", lambda x, y: 100, raising=False)
    monkeypatch.setattr(compat, "_foreground_pid", lambda: None, raising=False)
    monkeypatch.setattr(compat, "_check_uipi",
                        lambda pid: {"ok": True, "detail": "same level"},
                        raising=False)
    monkeypatch.setattr(compat, "_activate_window", lambda pid: False, raising=False)
    return control


def _shortcut_env(monkeypatch, *, foreground, activate_ok=True):
    """Wire the shortcut seams: one target pid 100, chosen foreground."""
    control = FakeControl(pid=100)
    monkeypatch.setattr(compat, "_resolve_control", lambda eid: control, raising=False)
    monkeypatch.setattr(compat, "_check_uipi",
                        lambda pid: {"ok": True, "detail": "same level"},
                        raising=False)
    state = {"fg": foreground}
    monkeypatch.setattr(compat, "_foreground_pid", lambda: state["fg"], raising=False)

    calls: list[int] = []

    def activate(pid):
        calls.append(pid)
        if activate_ok:
            state["fg"] = pid
            return True
        return False

    monkeypatch.setattr(compat, "_activate_window", activate, raising=False)
    return state, calls


# ------------------------------------------------------------------ refusals


def test_safe_click_without_frame_is_refused(spy, bound):
    result = _handler("click")({"x": 10, "y": 10, "target": "t1"})
    assert result["ok"] is False and result["error"] == "FRAME_REQUIRED", result
    assert spy == []


def test_safe_click_without_target_is_refused(spy, bound):
    frame = _register_frame()
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"]})
    assert result["ok"] is False and result["error"] == "TARGET_REQUIRED", result
    assert spy == []


def test_unknown_frame_refused_zero_input(spy, bound):
    result = _handler("click")({"x": 10, "y": 10, "frame": "f-deadbeef",
                                "target": "t1"})
    assert result["error"] == "FRAME_UNKNOWN", result
    assert spy == []


def test_stale_frame_refused_zero_input(spy, bound, monkeypatch):
    clock = {"ms": 1000.0}
    monkeypatch.setattr(vision, "_now_ms", lambda: clock["ms"], raising=False)
    frame = _register_frame(left=100, top=40)
    clock["ms"] += vision.FRAME_TTL_MS + 1
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"],
                                "target": "t1"})
    assert result["error"] == "FRAME_STALE", result
    assert spy == []


def test_point_outside_target_refused_zero_input(spy, bound):
    frame = _register_frame(left=100, top=40)
    # image x=300 + left=100 -> desktop x=400, outside rect (100,40,200,140)
    result = _handler("click")({"x": 300, "y": 10, "frame": frame["id"],
                                "target": "t1"})
    assert result["error"] == "TARGET_MISMATCH", result
    assert spy == []


def test_occluded_point_refused_zero_input(spy, bound, monkeypatch):
    monkeypatch.setattr(compat, "_hit_test_pid", lambda x, y: 999, raising=False)
    frame = _register_frame(left=100, top=40)
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"],
                                "target": "t1"})
    assert result["error"] == "OCCLUDED", result
    assert spy == []


def test_uipi_refusal_is_typed_and_sends_nothing(spy, bound, monkeypatch):
    monkeypatch.setattr(
        compat, "_check_uipi",
        lambda pid: {"ok": False, "code": "ELEVATION_REQUIRED",
                     "detail": "target integrity is higher"},
        raising=False,
    )
    frame = _register_frame(left=100, top=40)
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"],
                                "target": "t1"})
    assert result["error"] == "ELEVATION_REQUIRED", result
    assert spy == []


def test_unknown_target_refused_zero_input(spy, bound, monkeypatch):
    monkeypatch.setattr(compat, "_resolve_control", lambda eid: None, raising=False)
    frame = _register_frame(left=100, top=40)
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"],
                                "target": "t9"})
    assert result["error"] == "TARGET_NOT_FOUND", result
    assert spy == []


# ------------------------------------------------------------------ success


def test_bound_click_translates_and_sends(spy, bound):
    frame = _register_frame(left=100, top=40)
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"],
                                "target": "t1"})
    assert result["ok"] is True, result
    assert (result["x"], result["y"]) == (110.0, 50.0)
    assert spy == [[
        {"type": "mouse", "action": "move", "x": 110.0, "y": 50.0},
        {"type": "mouse", "action": "down", "button": "left"},
        {"type": "mouse", "action": "up", "button": "left"},
    ]]


def test_scaled_frame_translates_back(spy, bound):
    frame = _register_frame(left=100, top=40, scale=0.5)
    result = _handler("click")({"x": 10, "y": 10, "frame": frame["id"],
                                "target": "t1"})
    assert result["ok"] is True, result
    assert (result["x"], result["y"]) == (120.0, 60.0)


def test_center_click_uses_target_rect(spy, bound):
    result = _handler("click")({"target": "t1"})
    assert result["ok"] is True, result
    assert (result["x"], result["y"]) == (150.0, 90.0)
    assert spy[-1][0] == {"type": "mouse", "action": "move", "x": 150.0, "y": 90.0}
    assert [e["action"] for e in spy[-1][1:]] == ["down", "up"]


def test_cursor_click_needs_target_in_safe_mode(spy, bound):
    result = _handler("click")({})
    assert result["error"] == "TARGET_REQUIRED", result
    assert spy == []


# ------------------------------------------------------------------ profiles


def test_unrestricted_profile_restores_global_click(spy, bound):
    compat.set_unrestricted(True)
    result = _handler("click")({"x": 5, "y": 6})
    assert result["ok"] is True
    assert spy[-1] == [
        {"type": "mouse", "action": "move", "x": 5.0, "y": 6.0},
        {"type": "mouse", "action": "down", "button": "left"},
        {"type": "mouse", "action": "up", "button": "left"},
    ]


def test_unrestricted_still_refuses_a_stale_frame(spy, bound, monkeypatch):
    compat.set_unrestricted(True)
    clock = {"ms": 1000.0}
    monkeypatch.setattr(vision, "_now_ms", lambda: clock["ms"], raising=False)
    frame = _register_frame()
    clock["ms"] += vision.FRAME_TTL_MS + 1
    result = _handler("click")({"x": 1, "y": 2, "frame": frame["id"]})
    assert result["error"] == "FRAME_STALE", result
    assert spy == []


def test_unrestricted_still_verifies_a_named_target(spy, bound):
    compat.set_unrestricted(True)
    result = _handler("click")({"x": 500, "y": 500, "target": "t1"})
    assert result["error"] == "TARGET_MISMATCH", result
    assert spy == []


# ------------------------------------------------------------------ keyboard


def test_shortcut_requires_target_in_safe_mode(spy, bound):
    result = _handler("shortcut")({"keys": "ctrl+s"})
    assert result["error"] == "TARGET_REQUIRED", result
    assert spy == []


def test_shortcut_activates_target_when_not_foreground(spy, monkeypatch):
    _, calls = _shortcut_env(monkeypatch, foreground=777)
    result = _handler("shortcut")({"keys": "ctrl+s", "target": "t1"})
    assert result["ok"] is True, result
    assert result["activated"] is True
    assert calls == [100]
    assert spy and spy[-1][0]["type"] == "key"


def test_shortcut_activation_failure_is_refused(spy, monkeypatch):
    _shortcut_env(monkeypatch, foreground=777, activate_ok=False)
    result = _handler("shortcut")({"keys": "ctrl+s", "target": "t1"})
    assert result["error"] == "ACTIVATION_FAILED", result
    assert spy == []


def test_shortcut_foreground_target_sends_without_activation(spy, monkeypatch):
    _, calls = _shortcut_env(monkeypatch, foreground=100)
    result = _handler("shortcut")({"keys": "enter", "target": "t1"})
    assert result["ok"] is True and result["activated"] is False
    assert calls == []
    assert [e["vk"] for e in spy[-1]] == [0x0D, 0x0D]


def test_shortcut_activate_false_requires_foreground(spy, monkeypatch):
    _shortcut_env(monkeypatch, foreground=777)
    result = _handler("shortcut")({"keys": "ctrl+s", "target": "t1",
                                   "activate": False})
    assert result["error"] == "ACTIVATION_FAILED", result
    assert spy == []


# ------------------------------------------------------------- scroll / drag


def test_scroll_requires_anchor_in_safe_mode(spy, bound):
    result = _handler("scroll")({"direction": "down", "amount": 1})
    assert result["error"] == "TARGET_REQUIRED", result
    assert spy == []


def test_scroll_at_target_moves_then_wheels(spy, bound):
    result = _handler("scroll")({"direction": "down", "amount": 1, "target": "t1"})
    assert result["ok"] is True, result
    assert [e["action"] for e in spy[-1]] == ["move", "wheel"]
    assert spy[-1][0]["x"] == 150.0 and spy[-1][0]["y"] == 90.0


def test_drag_requires_binding_in_safe_mode(spy, bound):
    result = _handler("move")({"x": 10, "y": 10, "drag": True})
    assert result["error"] == "FRAME_REQUIRED", result
    assert spy == []


def test_bound_drag_verifies_both_points(spy, bound):
    frame = _register_frame(left=100, top=40)
    result = _handler("move")({
        "x": 50, "y": 50, "drag": True, "from_x": 20, "from_y": 20,
        "frame": frame["id"], "target": "t1",
    })
    assert result["ok"] is True, result
    events = spy[-1]
    assert events[0] == {"type": "mouse", "action": "move", "x": 120.0, "y": 60.0}
    assert events[1] == {"type": "mouse", "action": "down", "button": "left"}
    assert events[2] == {"type": "mouse", "action": "move", "x": 150.0, "y": 90.0}
    assert events[3] == {"type": "mouse", "action": "up", "button": "left"}


def test_safe_drag_without_from_is_refused(spy, bound):
    frame = _register_frame(left=100, top=40)
    result = _handler("move")({"x": 50, "y": 50, "drag": True,
                               "frame": frame["id"], "target": "t1"})
    assert result["error"] == "FROM_REQUIRED", result
    assert spy == []


def test_plain_move_is_allowed_unbound(spy):
    result = _handler("move")({"x": 5, "y": 6})
    assert result["ok"] is True
    assert spy[-1] == [{"type": "mouse", "action": "move", "x": 5.0, "y": 6.0}]


# ------------------------------------------------------------------ plumbing


def test_input_unrestricted_flag_switches_profile(monkeypatch):
    compat.set_unrestricted(False)
    calls: list[dict] = []
    monkeypatch.setattr(registry, "run_server", lambda **kwargs: calls.append(kwargs))
    try:
        assert server_main.main(["--input-unrestricted"]) == 0
        assert compat._unrestricted() is True
        server_main.main([])
        assert compat._unrestricted() is False
    finally:
        compat.set_unrestricted(False)


def test_dpi_awareness_helper_never_raises():
    server_main._enable_dpi_awareness()


def test_absolute_move_spans_the_virtual_desktop():
    # Regression: without MOUSEEVENTF_VIRTUALDESK the 0..65535 absolute
    # space maps onto the primary monitor only, so a click computed from a
    # full-virtual-desktop screenshot lands hundreds of pixels off on any
    # multi-monitor setup.
    record = compat._to_input({"type": "mouse", "action": "move", "x": 10, "y": 20})
    assert record.union.mi.dwFlags & compat.MOUSEEVENTF_ABSOLUTE
    assert record.union.mi.dwFlags & compat.MOUSEEVENTF_VIRTUALDESK
