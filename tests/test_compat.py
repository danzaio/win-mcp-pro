"""Tests for tools/compat.py (pointer/keyboard/clipboard compat tools).

Everything here runs with no real input: the DRY_RUN path is asserted to
perform nothing, and the real path is asserted against a monkeypatched
``_send_input`` / clipboard primitive so the ctypes win32 layer never
touches the machine.
"""
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.compat import TOOLS  # noqa: E402


# ---------------------------------------------------------------- helpers

def get_tool(name):
    for tool in TOOLS:
        if tool["name"] == name:
            return tool
    raise AssertionError(f"tool {name!r} not registered in TOOLS")


def model_of(name):
    return get_tool(name)["schema"]


def handler_of(name):
    return get_tool(name)["handler"]


@pytest.fixture
def real(monkeypatch):
    """Handlers with primitives captured; DRY_RUN off.

    These tests exercise the legacy global-input path, which the safe
    default profile now refuses; the explicit unrestricted profile is what
    a caller gets with ``--input-unrestricted``.
    """
    import tools.compat as compat

    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("WINMCP_INPUT_UNRESTRICTED", "1")
    captured = {"events": []}
    monkeypatch.setattr(
        compat, "_send_input", lambda evs: captured["events"].extend(evs)
    )
    monkeypatch.setattr(compat, "_clipboard_read", lambda: "stored text")
    monkeypatch.setattr(compat, "_clipboard_write", lambda text: captured.__setitem__("written", text))
    return captured


@pytest.fixture
def poisoned(monkeypatch):
    """Primitives that fail loudly if anything real fires."""
    import tools.compat as compat

    def boom(*a, **k):
        raise AssertionError("real input fired during dry run")

    monkeypatch.setattr(compat, "_send_input", boom)
    monkeypatch.setattr(compat, "_clipboard_read", boom)
    monkeypatch.setattr(compat, "_clipboard_write", boom)


def dry(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")


# ---------------------------------------------------------------- registry

def test_registry_shape():
    assert [t["name"] for t in TOOLS] == [
        "click",
        "move",
        "scroll",
        "shortcut",
        "clipboard_get",
        "clipboard_set",
    ]
    for tool in TOOLS:
        assert set(tool) == {"name", "description", "schema", "handler"}
        assert issubclass(tool["schema"], BaseModel)
        assert tool["description"].strip()
        assert callable(tool["handler"])


# ---------------------------------------------------------------- click

def test_click_args_valid():
    m = model_of("click")
    assert m.model_validate({"x": 10, "y": 20}).button == "left"
    assert m.model_validate({}).x is None  # current position
    assert m.model_validate({"x": 0, "y": 0, "button": "right"}).button == "right"
    assert m.model_validate({"button": "Middle"}).button == "middle"


@pytest.mark.parametrize("bad", [
    {"button": "left2"},
    {"button": ""},
    {"x": "abc"},
    {"x": True},
    {"x": 5},  # y missing: mixed coords
    {"y": 5},
])
def test_click_args_invalid(bad):
    with pytest.raises(ValidationError):
        model_of("click").model_validate(bad)


def test_negative_coordinates_are_valid():
    # Multi-monitor virtual desktops have monitors left of/above the
    # primary one, so negative desktop coordinates must validate.
    m = model_of("click")
    assert m.model_validate({"x": -1920, "y": -200}).x == -1920.0
    assert m.model_validate({"x": 5, "y": -3}).y == -3.0


def test_click_dry_run(poisoned, monkeypatch):
    dry(monkeypatch)
    result = handler_of("click")({"x": 12, "y": 34, "button": "right"})
    assert result == {
        "dry_run": True,
        "action": {"name": "click", "x": 12.0, "y": 34.0, "button": "right",
                   "target": None, "frame": None},
    }


def test_click_dry_run_no_position(poisoned, monkeypatch):
    dry(monkeypatch)
    result = handler_of("click")({})
    assert result["action"] == {"name": "click", "x": None, "y": None,
                                "button": "left", "target": None, "frame": None}


def test_click_real_at_position(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    result = handler_of("click")({"x": 12, "y": 34})
    assert result["ok"] is True
    assert result["x"] == 12.0 and result["y"] == 34.0 and result["button"] == "left"
    assert real["events"] == [
        {"type": "mouse", "action": "move", "x": 12.0, "y": 34.0},
        {"type": "mouse", "action": "down", "button": "left"},
        {"type": "mouse", "action": "up", "button": "left"},
    ]


def test_click_real_right_button(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    handler_of("click")({"x": 1, "y": 2, "button": "right"})
    assert real["events"][1] == {"type": "mouse", "action": "down", "button": "right"}
    assert real["events"][2] == {"type": "mouse", "action": "up", "button": "right"}


def test_click_real_without_coords_uses_cursor(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr("tools.compat._cursor_pos", lambda: (7, 9))
    handler_of("click")({})
    assert real["events"] == [
        {"type": "mouse", "action": "down", "button": "left"},
        {"type": "mouse", "action": "up", "button": "left"},
    ]


# ---------------------------------------------------------------- move

def test_move_args_valid():
    m = model_of("move")
    a = m.model_validate({"x": 5, "y": 6})
    assert a.drag is False and a.from_x is None
    b = m.model_validate({"x": 5, "y": 6, "drag": True, "from_x": 1, "from_y": 2})
    assert b.drag and b.from_x == 1.0


@pytest.mark.parametrize("bad", [
    {},
    {"x": 1},
    {"y": 1},
    {"x": 1, "y": 1, "from_x": 3},  # from_y missing alongside from_x
    {"x": 1, "y": 1, "drag": "maybe"},
    {"x": 1, "y": 1, "drag": 2},
])
def test_move_args_invalid(bad):
    with pytest.raises(ValidationError):
        model_of("move").model_validate(bad)


def test_move_dry_run(poisoned, monkeypatch):
    dry(monkeypatch)
    result = handler_of("move")({"x": 3, "y": 4, "drag": True, "from_x": 1, "from_y": 1})
    assert result == {
        "dry_run": True,
        "action": {
            "name": "move",
            "x": 3.0,
            "y": 4.0,
            "drag": True,
            "from_x": 1.0,
            "from_y": 1.0,
            "target": None,
            "frame": None,
        },
    }


def test_move_real_plain(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    handler_of("move")({"x": 100, "y": 200})
    assert real["events"] == [{"type": "mouse", "action": "move", "x": 100.0, "y": 200.0}]


def test_move_real_drag_from_explicit_start(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    result = handler_of("move")({"x": 100, "y": 200, "drag": True, "from_x": 10, "from_y": 20})
    assert result["ok"] is True
    assert real["events"] == [
        {"type": "mouse", "action": "move", "x": 10.0, "y": 20.0},
        {"type": "mouse", "action": "down", "button": "left"},
        {"type": "mouse", "action": "move", "x": 100.0, "y": 200.0},
        {"type": "mouse", "action": "up", "button": "left"},
    ]


def test_move_real_drag_from_cursor(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr("tools.compat._cursor_pos", lambda: (11, 22))
    handler_of("move")({"x": 100, "y": 200, "drag": True})
    assert real["events"][0] == {"type": "mouse", "action": "move", "x": 11.0, "y": 22.0}
    assert real["events"][1] == {"type": "mouse", "action": "down", "button": "left"}


# ---------------------------------------------------------------- scroll

def test_scroll_args_valid():
    m = model_of("scroll")
    assert m.model_validate({"direction": "up", "amount": 3}).amount == 3
    assert m.model_validate({"direction": "LEFT", "amount": 1}).direction == "left"


@pytest.mark.parametrize("bad", [
    {},
    {"direction": "up"},
    {"amount": 2},
    {"direction": "sideways", "amount": 1},
    {"direction": "up", "amount": 0},
    {"direction": "up", "amount": -2},
    {"direction": "up", "amount": 1.5},
])
def test_scroll_args_invalid(bad):
    with pytest.raises(ValidationError):
        model_of("scroll").model_validate(bad)


def test_scroll_dry_run(poisoned, monkeypatch):
    dry(monkeypatch)
    result = handler_of("scroll")({"direction": "down", "amount": 2})
    assert result == {
        "dry_run": True,
        "action": {"name": "scroll", "direction": "down", "amount": 2,
                   "x": None, "y": None, "target": None, "frame": None},
    }


@pytest.mark.parametrize("direction,delta,horizontal", [
    ("up", 120, False),
    ("down", -120, False),
    ("right", 240, True),
    ("left", -240, True),
])
def test_scroll_real_delta(real, monkeypatch, direction, delta, horizontal):
    monkeypatch.setenv("DRY_RUN", "0")
    amount = 2 if direction in ("right", "left") else 1
    handler_of("scroll")({"direction": direction, "amount": amount})
    assert real["events"] == [
        {
            "type": "mouse",
            "action": "wheel",
            "delta": delta,
            "horizontal": horizontal,
        }
    ]


# ---------------------------------------------------------------- shortcut

def test_shortcut_args_valid():
    m = model_of("shortcut")
    assert m.model_validate({"keys": "Ctrl+S"}).keys == "ctrl+s"
    assert m.model_validate({"keys": "alt+f4"}).keys == "alt+f4"
    assert m.model_validate({"keys": "ctrl+shift+p"}).keys == "ctrl+shift+p"


@pytest.mark.parametrize("bad", [
    {},
    {"keys": ""},
    {"keys": "   "},
    {"keys": "ctrl++s"},
    {"keys": "+ctrl+s"},
    {"keys": "ctrl+s+"},
    {"keys": "ctrl s"},
    {"keys": "ctrl-s"},
    {"keys": "a" * 16},
    {"keys": ("verylongtoken%d" % 12345)},
    {"keys": 5},
])
def test_shortcut_args_invalid(bad):
    with pytest.raises(ValidationError):
        model_of("shortcut").model_validate(bad)


def test_shortcut_dry_run(poisoned, monkeypatch):
    dry(monkeypatch)
    result = handler_of("shortcut")({"keys": "Ctrl+Shift+S"})
    assert result == {
        "dry_run": True,
        "action": {"name": "shortcut", "keys": "ctrl+shift+s",
                   "target": None, "activate": True},
    }


def test_shortcut_real_order(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    handler_of("shortcut")({"keys": "ctrl+shift+p"})
    assert real["events"] == [
        {"type": "key", "vk": 0x11, "up": False},
        {"type": "key", "vk": 0x10, "up": False},
        {"type": "key", "vk": 0x50, "up": False},
        {"type": "key", "vk": 0x50, "up": True},
        {"type": "key", "vk": 0x10, "up": True},
        {"type": "key", "vk": 0x11, "up": True},
    ]


def test_shortcut_real_named_keys(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    handler_of("shortcut")({"keys": "alt+f4"})
    assert [e["vk"] for e in real["events"]] == [0x12, 0x73, 0x73, 0x12]
    handler_of("shortcut")({"keys": "win+d"})
    assert [e["vk"] for e in real["events"][-4:]] == [0x5B, 0x44, 0x44, 0x5B]


def test_shortcut_rejects_unknown_named_key(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    with pytest.raises(ValueError):
        handler_of("shortcut")({"keys": "ctrl+nosuchkey"})


# ---------------------------------------------------------------- clipboard

def test_clipboard_get_args_empty():
    assert model_of("clipboard_get").model_validate({}) is not None
    assert model_of("clipboard_get").model_validate({"anything": 1}).model_dump() == {}


def test_clipboard_get_dry_run(poisoned, monkeypatch):
    dry(monkeypatch)
    assert handler_of("clipboard_get")({}) == {
        "dry_run": True,
        "action": {"name": "clipboard_get"},
    }


def test_clipboard_get_real(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    assert handler_of("clipboard_get")({}) == {"ok": True, "text": "stored text"}


def test_clipboard_set_args():
    m = model_of("clipboard_set")
    assert m.model_validate({"text": "hello"}).text == "hello"
    assert m.model_validate({"text": ""}).text == ""
    assert m.model_validate({"text": "x" * 100_000}).text == "x" * 100_000


@pytest.mark.parametrize("bad", [{}, {"text": None}, {"text": 5}, {"text": "x" * 100001}])
def test_clipboard_set_args_invalid(bad):
    with pytest.raises(ValidationError):
        model_of("clipboard_set").model_validate(bad)


def test_clipboard_set_dry_run(poisoned, monkeypatch):
    dry(monkeypatch)
    result = handler_of("clipboard_set")({"text": "hi"})
    assert result == {
        "dry_run": True,
        "action": {"name": "clipboard_set", "text": "hi"},
    }


def test_clipboard_set_real(real, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    assert handler_of("clipboard_set")({"text": "hi"}) == {"ok": True, "chars": 2}
    assert real["written"] == "hi"


# ---------------------------------------------------------------- dry-run gate

@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("on", True),
    ("0", False),
    ("", False),
    ("false", False),
])
def test_dry_run_gate_values(real, monkeypatch, value, expected):
    monkeypatch.setenv("DRY_RUN", value)
    result = handler_of("scroll")({"direction": "up", "amount": 1})
    assert bool(result.get("dry_run", False)) is expected
    if not expected:
        assert real["events"] == [
            {"type": "mouse", "action": "wheel", "delta": 120, "horizontal": False}
        ]
