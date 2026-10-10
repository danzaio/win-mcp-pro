"""Task 5 tests: Control Pattern actions (invoke/toggle/select/expand/scroll_into_view/focus).

No real UIA, no real input: every UIA surface is a fake in this file.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # keep the slice self-contained (repo has no packages yet)
    sys.path.insert(0, str(ROOT))

from tools import patterns  # noqa: E402

# UIA pattern ids (stable, documented in PatternId of the uiautomation package).
PID = {
    "invoke": 10000,
    "expand_collapse": 10005,
    "selection_item": 10010,
    "toggle": 10015,
    "scroll_item": 10017,
}
EXPECTED_NAMES = {"invoke", "toggle", "select", "expand", "scroll_into_view", "focus"}
SNAPSHOT_KEYS = {
    "element_id",
    "name",
    "control_type",
    "automation_id",
    "class_name",
    "enabled",
    "offscreen",
    "focused",
    "toggle_state",
    "is_selected",
    "expand_state",
    "patterns",
}


class FakeInvoke:
    def __init__(self, el):
        self.el = el

    def Invoke(self):
        self.el.calls.append("Invoke")
        self.el.invoked += 1


class FakeToggle:
    def __init__(self, el):
        self.el = el

    @property
    def ToggleState(self):
        return self.el.toggle_state

    def Toggle(self):
        self.el.calls.append("Toggle")
        if "no_effect" in self.el.modes:
            return
        self.el.toggle_state = 1 - self.el.toggle_state


class FakeSelectionItem:
    def __init__(self, el):
        self.el = el

    @property
    def IsSelected(self):
        return self.el.selected

    def Select(self):
        self.el.calls.append("Select")
        if "no_effect" in self.el.modes:
            return
        self.el.selected = True


class FakeExpandCollapse:
    def __init__(self, el):
        self.el = el

    @property
    def ExpandCollapseState(self):
        return self.el.expand_state

    def Expand(self):
        self.el.calls.append("Expand")
        if "no_effect" in self.el.modes:
            return
        self.el.expand_state = 1

    def Collapse(self):
        self.el.calls.append("Collapse")
        self.el.expand_state = 0


class FakeScrollItem:
    def __init__(self, el):
        self.el = el

    def ScrollIntoView(self):
        self.el.calls.append("ScrollIntoView")
        self.el.offscreen = False


class FakeElement:
    """Duck-typed stand-in for uiautomation.Control."""

    def __init__(
        self,
        elem_id="321",
        hwnd=100,
        supported=("invoke",),
        enabled=True,
        offscreen=False,
        focused=False,
        toggle_state=0,
        selected=False,
        expand_state=0,
        modes=(),
        name="OK button",
    ):
        self.elem_id = elem_id
        self.hwnd = hwnd
        self.supported = tuple(supported)
        self.enabled = enabled
        self.offscreen = offscreen
        self.focused = focused
        self.toggle_state = toggle_state
        self.selected = selected
        self.expand_state = expand_state
        self.modes = tuple(modes)
        self.name = name
        self.calls: list[str] = []
        self.invoked = 0

    # -- identity -------------------------------------------------------
    def GetRuntimeId(self):
        return [self.hwnd, 7, int(self.elem_id)]

    # -- pattern getters (uiautomation returns None when unsupported) ---
    def GetInvokePattern(self):
        return FakeInvoke(self) if "invoke" in self.supported else None

    def GetTogglePattern(self):
        return FakeToggle(self) if "toggle" in self.supported else None

    def GetSelectionItemPattern(self):
        return FakeSelectionItem(self) if "selection_item" in self.supported else None

    def GetExpandCollapsePattern(self):
        return FakeExpandCollapse(self) if "expand_collapse" in self.supported else None

    def GetScrollItemPattern(self):
        return FakeScrollItem(self) if "scroll_item" in self.supported else None

    def GetPattern(self, pattern_id):
        table = {
            PID["invoke"]: self.GetInvokePattern,
            PID["toggle"]: self.GetTogglePattern,
            PID["selection_item"]: self.GetSelectionItemPattern,
            PID["expand_collapse"]: self.GetExpandCollapsePattern,
            PID["scroll_item"]: self.GetScrollItemPattern,
        }
        getter = table.get(int(pattern_id))
        return getter() if getter else None

    def SetFocus(self):
        self.calls.append("SetFocus")
        if "focus_refused" in self.modes:
            return False
        self.focused = True

    # -- plain properties ------------------------------------------------
    @property
    def Name(self):
        return self.name

    @property
    def ClassName(self):
        return "Fake"

    @property
    def AutomationId(self):
        return "fake-" + str(self.elem_id)

    @property
    def ControlTypeName(self):
        return "ButtonControl"

    @property
    def IsEnabled(self):
        return self.enabled

    @property
    def IsOffscreen(self):
        return self.offscreen

    @property
    def HasKeyboardFocus(self):
        return self.focused

    @property
    def NativeWindowHandle(self):
        return self.hwnd


class FakeNode:
    """Tree node used to exercise the runtime-id walk."""

    def __init__(self, rid, children=()):
        self.rid = tuple(int(x) for x in rid)
        self.children = list(children)

    def GetRuntimeId(self):
        return list(self.rid)

    def GetChildren(self):
        return list(self.children)


class FakeUia:
    def __init__(self, tree=None):
        self.tree = tree

    def ControlFromHandle(self, handle):
        if self.tree is not None and self.tree.rid and self.tree.rid[0] == int(handle):
            return self.tree
        return None


class FakeCOMError(Exception):
    """Shape-matched stand-in for comtypes.COMError (hresult attribute)."""

    def __init__(self, hresult, *args):
        super().__init__(*args)
        self.hresult = hresult
        self.args = args


def _wire(monkeypatch, element, element_id="100:7-321"):
    monkeypatch.setattr(patterns, "resolve_element", lambda eid: element)
    return element_id


# --------------------------------------------------------------------------
# registry / schema contract
# --------------------------------------------------------------------------
def test_tools_registry_contract():
    from pydantic import BaseModel

    assert {t.name for t in patterns.TOOLS} == EXPECTED_NAMES
    for tool in patterns.TOOLS:
        assert tool.name and tool.description
        assert issubclass(tool.schema, BaseModel)
        assert "id" in tool.schema.model_fields
        assert callable(tool.handler)
        assert tool.schema.model_json_schema()["properties"]  # JSON Schema exportable


def test_action_args_requires_id():
    with pytest.raises(Exception):
        patterns.ActionArgs.model_validate({})


def test_handlers_return_json_safe_dict(monkeypatch, capsys):
    el = FakeElement(supported=("invoke", "toggle", "selection_item", "expand_collapse", "scroll_item"))
    _wire(monkeypatch, el)
    for tool in patterns.TOOLS:
        out = tool.handler("100:7-321")
        assert isinstance(out, dict), tool.name
        json.dumps(out)  # must be serializable
        out2 = tool.handler(id="100:7-321")
        json.dumps(out2)
    assert capsys.readouterr().out == ""


def test_handler_accepts_payload_dict(monkeypatch):
    el = FakeElement()
    _wire(monkeypatch, el)
    result = patterns.TOOLS[0].handler(payload={"id": "100:7-321"})
    assert result["ok"] is True


# --------------------------------------------------------------------------
# invoke
# --------------------------------------------------------------------------
def test_invoke_returns_before_after_shape(monkeypatch):
    el = FakeElement()
    _wire(monkeypatch, el)
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert set(result) >= {"ok", "before", "after", "action", "error"}
    assert result["ok"] is True
    assert result["error"] is None
    assert set(result["before"]) == SNAPSHOT_KEYS
    assert set(result["after"]) == SNAPSHOT_KEYS
    assert el.calls == ["Invoke"]


def test_invoke_pattern_unsupported(monkeypatch):
    el = FakeElement(supported=())
    _wire(monkeypatch, el)
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "PATTERN_UNSUPPORTED"
    assert el.calls == []
    assert result["before"] == result["after"]


def test_invoke_on_disabled_element_is_rejected(monkeypatch):
    el = FakeElement(enabled=False)
    _wire(monkeypatch, el)
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "ELEMENT_DISABLED"
    assert el.calls == []


def test_invoke_access_denied_maps_to_elevation_required(monkeypatch):
    el = FakeElement()

    class Denying(FakeInvoke):
        def Invoke(self):
            raise FakeCOMError(0x80070005, "E_ACCESSDENIED")

    el.GetInvokePattern = lambda: Denying(el)
    _wire(monkeypatch, el)
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "ELEVATION_REQUIRED"


def test_invoke_generic_failure_is_typed(monkeypatch):
    el = FakeElement()

    class Broken(FakeInvoke):
        def Invoke(self):
            raise RuntimeError("provider died")

    el.GetInvokePattern = lambda: Broken(el)
    _wire(monkeypatch, el)
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "PATTERN_FAILED"


# --------------------------------------------------------------------------
# toggle / select / expand / scroll / focus
# --------------------------------------------------------------------------
def test_toggle_flips_state_in_after(monkeypatch):
    el = FakeElement(supported=("toggle",), toggle_state=0)
    _wire(monkeypatch, el)
    result = patterns.run_action("toggle", "100:7-321").model_dump()
    assert result["ok"] is True
    assert result["before"]["toggle_state"] == "off"
    assert result["after"]["toggle_state"] == "on"
    assert el.calls == ["Toggle"]


def test_toggle_pattern_unsupported(monkeypatch):
    el = FakeElement(supported=())
    _wire(monkeypatch, el)
    result = patterns.run_action("toggle", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "PATTERN_UNSUPPORTED"


def test_toggle_without_effect_is_reported(monkeypatch):
    el = FakeElement(supported=("toggle",), modes=("no_effect",))
    _wire(monkeypatch, el)
    result = patterns.run_action("toggle", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_NO_EFFECT"
    assert el.calls == ["Toggle"]


def test_select_marks_selection(monkeypatch):
    el = FakeElement(supported=("selection_item",), selected=False)
    _wire(monkeypatch, el)
    result = patterns.run_action("select", "100:7-321").model_dump()
    assert result["ok"] is True
    assert result["before"]["is_selected"] is False
    assert result["after"]["is_selected"] is True
    assert el.calls == ["Select"]


def test_select_pattern_unsupported(monkeypatch):
    el = FakeElement(supported=())
    _wire(monkeypatch, el)
    result = patterns.run_action("select", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "PATTERN_UNSUPPORTED"


def test_expand_expands(monkeypatch):
    el = FakeElement(supported=("expand_collapse",), expand_state=0)
    _wire(monkeypatch, el)
    result = patterns.run_action("expand", "100:7-321").model_dump()
    assert result["ok"] is True
    assert result["before"]["expand_state"] == "collapsed"
    assert result["after"]["expand_state"] == "expanded"
    assert el.calls == ["Expand"]


def test_expand_pattern_unsupported(monkeypatch):
    el = FakeElement(supported=())
    _wire(monkeypatch, el)
    result = patterns.run_action("expand", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "PATTERN_UNSUPPORTED"


def test_scroll_into_view_clears_offscreen(monkeypatch):
    el = FakeElement(supported=("scroll_item",), offscreen=True)
    _wire(monkeypatch, el)
    result = patterns.run_action("scroll_into_view", "100:7-321").model_dump()
    assert result["ok"] is True
    assert result["before"]["offscreen"] is True
    assert result["after"]["offscreen"] is False
    assert el.calls == ["ScrollIntoView"]


def test_scroll_into_view_pattern_unsupported(monkeypatch):
    el = FakeElement(supported=())
    _wire(monkeypatch, el)
    result = patterns.run_action("scroll_into_view", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "PATTERN_UNSUPPORTED"


def test_focus_takes_focus(monkeypatch):
    el = FakeElement()
    _wire(monkeypatch, el)
    result = patterns.run_action("focus", "100:7-321").model_dump()
    assert result["ok"] is True
    assert result["before"]["focused"] is False
    assert result["after"]["focused"] is True
    assert el.calls == ["SetFocus"]


def test_focus_refused_is_reported(monkeypatch):
    el = FakeElement(modes=("focus_refused",))
    _wire(monkeypatch, el)
    result = patterns.run_action("focus", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_NO_EFFECT"


# --------------------------------------------------------------------------
# resolution: ids "hwnd:runtimeid"
# --------------------------------------------------------------------------
def test_invalid_id_is_typed(monkeypatch):
    monkeypatch.setattr(patterns, "_ELEMENTS", {})
    for bad in ["", "100", "abc:7-321", "100:", None, 42]:
        result = patterns.run_action("invoke", bad).model_dump()
        assert result["ok"] is False, bad
        assert result["error"]["code"] == "INVALID_ID", bad


def test_missing_element_returns_not_found(monkeypatch):
    monkeypatch.setattr(patterns, "_ELEMENTS", {})
    monkeypatch.setattr(patterns, "_uia", None)  # no uiautomation available either
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


def test_resolve_prefers_registry(monkeypatch):
    el = FakeElement(elem_id="9", hwnd=55, supported=("invoke",))
    monkeypatch.setattr(patterns, "_ELEMENTS", {})
    patterns.register_element("55:9", el)
    assert patterns.resolve_element("55:9") is el


def test_resolve_walks_tree_for_runtime_id(monkeypatch):
    leaf = FakeNode((100, 7, 321))
    other = FakeNode((100, 7, 999))
    root = FakeNode((100,), [FakeNode((100, 1), [other, leaf]), FakeNode((100, 2), [])])
    monkeypatch.setattr(patterns, "_ELEMENTS", {})
    monkeypatch.setattr(patterns, "_uia", FakeUia(root))
    assert patterns.resolve_element("100:7,321") is leaf
    assert patterns.resolve_element("100:7.999") is other


def test_resolve_walk_respects_node_budget(monkeypatch):
    """A pathological provider must not hang the call."""

    class Infinite:
        rid = (1,)  # so ControlFromHandle hands it back as the window root

        def GetRuntimeId(self):
            return [1, 1]

        def GetChildren(self):
            while True:  # unbounded tree
                yield FakeNode((1, 1))

    monkeypatch.setattr(patterns, "_ELEMENTS", {})
    monkeypatch.setattr(patterns, "_uia", FakeUia(Infinite()))
    result = patterns.run_action("invoke", "1:1,1").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"
    assert "budget" in result["error"]["message"]


def test_unknown_action_is_rejected(monkeypatch):
    _wire(monkeypatch, FakeElement())
    result = patterns.run_action("teleport", "100:7-321").model_dump()
    assert result["ok"] is False
    assert result["error"]["code"] == "UNKNOWN_ACTION"


def test_repeated_invocation_is_idempotent_in_log(monkeypatch):
    el = FakeElement()
    _wire(monkeypatch, el)
    patterns.run_action("invoke", "100:7-321")
    patterns.run_action("invoke", "100:7-321")
    assert el.calls == ["Invoke", "Invoke"]  # exactly one effect per call


# --------------------------------------------------------------------------
# snapshot content
# --------------------------------------------------------------------------
def test_snapshot_reports_supported_patterns(monkeypatch):
    el = FakeElement(supported=("invoke", "toggle"))
    _wire(monkeypatch, el)
    result = patterns.run_action("invoke", "100:7-321").model_dump()
    assert sorted(result["after"]["patterns"]) == ["invoke", "toggle"]
    assert result["after"]["name"] == "OK button"
    assert result["after"]["enabled"] is True
    assert result["after"]["element_id"] == "100:7-321"


def test_snapshot_tolerates_unreadable_properties(monkeypatch):
    class Unreadable(FakeElement):
        """Every property access raises, the way a stale UIA element does."""

        def GetInvokePattern(self):
            raise FakeCOMError(0x80040201, "stale element")  # UIA_E_ELEMENTNOTAVAILABLE

        @property
        def Name(self):
            raise FakeCOMError(0x80040201, "stale element")

        @property
        def IsEnabled(self):
            raise FakeCOMError(0x80040201, "stale element")

        @property
        def ToggleState(self):
            raise FakeCOMError(0x80040201, "stale element")

    el = Unreadable()
    _wire(monkeypatch, el)
    snap = patterns.snapshot(el)
    assert snap["name"] is None
    assert snap["enabled"] is None
    assert snap["patterns"] == []


def test_resolve_digest_id_from_registry_without_parsing(monkeypatch):
    """find-layer digest ids resolve via the registry, never the parser."""
    monkeypatch.setattr(patterns, "_ELEMENTS", {})
    monkeypatch.setattr(patterns, "_uia", None)
    el = object()
    patterns.register_element("a1b2c3d4e5f6", el)
    assert patterns.resolve_element("a1b2c3d4e5f6") is el
