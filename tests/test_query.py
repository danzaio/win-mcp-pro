"""Tests for tools/query.py — find / dump_tree / get_prop / get_patterns.

The UIA layer is a fake shipped inline (no Windows, no `uiautomation` needed).
The fake records every traversal hop so the tests can prove the search is
chained one level at a time instead of a single deep search from the root.
"""

import pytest

from tools import query


# --------------------------------------------------------------------------
# fake UIA tree
# --------------------------------------------------------------------------


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeControl:
    """Minimal stand-in for uiautomation.Control.

    ``hops`` records the name of every control whose children were requested
    (one GetChildren call == one searchDepth=1 hop).
    """

    def __init__(self, name="", automation_id="", class_name="", control_type="",
                 handle=0, children=None, patterns=None, value=None):
        self.Name = name
        self.AutomationId = automation_id
        self.ClassName = class_name
        self.ControlTypeName = control_type
        self.NativeWindowHandle = handle
        self.hops = []
        self._children = list(children or [])
        self._patterns = set(patterns or [])
        self._value = value
        self.IsEnabled = True
        self.ProcessId = 4242

    def GetChildren(self):
        self.hops.append(self.Name)
        return list(self._children)

    # pattern probes in the shape of the real library ---------------------
    def GetInvokePattern(self):
        return self._probe("invoke")

    def GetValuePattern(self):
        return self._probe("value")

    def GetTogglePattern(self):
        return self._probe("toggle")

    def GetExpandCollapsePattern(self):
        return self._probe("expand_collapse")

    def GetSelectionPattern(self):
        return self._probe("selection")

    def GetSelectionItemPattern(self):
        return self._probe("selection_item")

    def GetScrollItemPattern(self):
        return self._probe("scroll_item")

    def GetWindowPattern(self):
        return self._probe("window")

    def GetTextPattern(self):
        return self._probe("text")

    def GetRangeValuePattern(self):
        return self._probe("range_value")

    def GetTransformPattern(self):
        return self._probe("transform")

    def GetScrollPattern(self):
        return self._probe("scroll")

    def GetGridPattern(self):
        return self._probe("grid")

    def GetGridItemPattern(self):
        return self._probe("grid_item")

    def GetTablePattern(self):
        return self._probe("table")

    def GetTableItemPattern(self):
        return self._probe("table_item")

    def GetMultipleViewPattern(self):
        return self._probe("multiple_view")

    def GetDockPattern(self):
        return self._probe("dock")

    def GetItemContainerPattern(self):
        return self._probe("item_container")

    def _probe(self, pattern):
        return object() if pattern in self._patterns else None


class FakeRoot(FakeControl):
    """Desktop root: exposes ONLY GetChildren, exactly like a depth-1 chain.

    A depth-3-from-root implementation would need a FindAll/search API here,
    which this class deliberately does not have.
    """


def build_tree():
    """Notepad-shaped fake: root -> window -> pane -> edit (+ depth-4 node)."""
    deep_edit = FakeControl(name="deep editor", control_type="EditControl")
    inner_pane = FakeControl(name="Pane2", control_type="PaneControl", children=[deep_edit])
    editor = FakeControl(
        name="Text Editor",
        automation_id="15",
        class_name="RichEditD2DPT",
        control_type="EditControl",
        patterns={"value", "text"},
        value="hello",
    )
    pane = FakeControl(name="Pane", control_type="PaneControl", children=[editor, inner_pane])
    window = FakeControl(name="Notepad", control_type="WindowControl", handle=1234,
                         children=[pane])
    root = FakeRoot(name="Desktop", control_type="WindowControl", children=[window])
    return root, window, pane, editor, deep_edit


class FakeUia:
    """Fake `uiautomation` module."""

    def __init__(self, root):
        self._root = root

    def GetRootControl(self):
        return self._root

    def GetForegroundWindow(self):
        return self._root.GetChildren()[0]


@pytest.fixture()
def fake_uia(monkeypatch):
    root, window, pane, editor, deep_edit = build_tree()
    monkeypatch.setattr(query, "_uia", FakeUia(root))
    query._registry_clear()
    return root, window, pane, editor, deep_edit


# --------------------------------------------------------------------------
# find
# --------------------------------------------------------------------------


def test_find_notepad_edit_via_chained_depth_one(fake_uia):
    """Required by the plan: chained window->parent->child finds the edit."""
    root, window, pane, editor, _ = fake_uia
    result = query.find(name="Text Editor")
    assert result["ok"] is True, result
    element = result["element"]
    assert element["name"] == "Text Editor"
    assert element["kind"] == "Edit"
    assert "Edit" in element["path"]
    assert element["path"] == "Window:Notepad > Pane > Edit:Text Editor"

    # one hop out of the window, then one hop out of the pane, never a
    # depth-3-from-root search (the fake root has no such API at all)
    assert window.hops[0] == "Pane"
    assert pane.hops == ["Text Editor", "Pane2"]
    assert root.hops == ["Notepad"]


def test_find_prefers_shallow_match_over_deep_duplicate():
    """BFS order: the shallowest candidate wins, deep duplicates are not needed."""
    deep = FakeControl(name="Save", control_type="ButtonControl")
    inner = FakeControl(name="Pane2", control_type="PaneControl", children=[deep])
    shallow = FakeControl(name="Save", control_type="ButtonControl")
    pane = FakeControl(name="Pane", control_type="PaneControl", children=[shallow, inner])
    window = FakeControl(name="Notepad", control_type="WindowControl", children=[pane])
    root = FakeRoot(name="Desktop", control_type="WindowControl", children=[window])
    query._uia = FakeUia(root)
    query._registry_clear()
    try:
        result = query.find(name="Save")
        assert result["ok"] is True
        assert result["element"]["path"] == "Window:Notepad > Pane > Button:Save"
        assert inner.hops == []  # the deep branch was never expanded
    finally:
        query._uia = None
        query._registry_clear()


def test_find_by_automation_id_class_name_and_kind(fake_uia):
    by_id = query.find(automation_id="15")
    assert by_id["ok"] is True and by_id["element"]["name"] == "Text Editor"

    by_class = query.find(class_name="RichEditD2DPT")
    assert by_class["ok"] is True and by_class["element"]["class_name"] == "RichEditD2DPT"

    by_kind = query.find(kind="edit")
    assert by_kind["ok"] is True and by_kind["element"]["kind"] == "Edit"


def test_find_criteria_are_ANDed(fake_uia):
    result = query.find(name="Text Editor", kind="Button")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"


def test_find_reaches_nodes_below_depth_three(fake_uia):
    """Depth-4 node: a depth-3-from-root search could never see it."""
    result = query.find(name="deep editor")
    assert result["ok"] is True, result
    assert result["element"]["path"].endswith("Edit:deep editor")


def test_find_missing_returns_typed_not_found(fake_uia):
    result = query.find(name="nonexistent control")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"
    assert result["reason"]
    assert "screenshot" in result["suggestion"].lower()


def test_find_without_criteria_is_bad_request(fake_uia):
    result = query.find()
    assert result["ok"] is False
    assert result["error"] == "BAD_REQUEST"


# --------------------------------------------------------------------------
# dump_tree
# --------------------------------------------------------------------------


def test_dump_tree_returns_nested_children_with_ids(fake_uia):
    root, window, pane, editor, deep_edit = fake_uia
    found = query.find(name="Notepad")
    assert found["ok"] is True
    window_id = found["element"]["id"]

    tree = query.dump_tree(window_id, depth=2)
    assert tree["ok"] is True, tree
    assert tree["element"]["id"] == window_id
    kinds = [c["kind"] for c in tree["children"]]
    assert kinds == ["Pane"]
    edit_node = tree["children"][0]["children"][0]
    assert edit_node["name"] == "Text Editor"

    # every dumped node is addressable afterwards via its own id
    prop = query.get_prop(edit_node["id"], "name")
    assert prop["ok"] is True and prop["value"] == "Text Editor"


def test_dump_tree_respects_depth(fake_uia):
    root, window, pane, editor, deep_edit = fake_uia
    tree = query.dump_tree(query.find(name="Notepad")["element"]["id"], depth=1)
    assert tree["ok"] is True
    assert tree["children"][0]["name"] == "Pane"
    assert tree["children"][0]["children"] == []


def test_dump_tree_rejects_absurd_depth(fake_uia):
    result = query.dump_tree(query.find(name="Notepad")["element"]["id"], depth=99)
    assert result["ok"] is False
    assert result["error"] == "BAD_REQUEST"


def test_dump_tree_unknown_id(fake_uia):
    result = query.dump_tree("nope-nope")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"
    assert result["reason"]


# --------------------------------------------------------------------------
# get_prop
# --------------------------------------------------------------------------


def test_get_prop_scalars_rect_and_value(fake_uia):
    element_id = query.find(name="Text Editor")["element"]["id"]

    assert query.get_prop(element_id, "name")["value"] == "Text Editor"
    assert query.get_prop(element_id, "kind")["value"] == "Edit"
    assert query.get_prop(element_id, "handle")["value"] == 1234
    assert query.get_prop(element_id, "pid")["value"] == 4242
    assert query.get_prop(element_id, "is_enabled")["value"] is True

    # rect is read off the window handle, as JSON-serializable dict
    editor = fake_uia[3]
    editor.BoundingRectangle = FakeRect(10, 20, 30, 40)
    rect = query.get_prop(element_id, "rect")
    assert rect["ok"] is True
    assert rect["value"] == {"left": 10, "top": 20, "right": 30, "bottom": 40}

    assert query.get_prop(element_id, "value")["value"] == "hello"


def test_get_prop_unknown_property_lists_available(fake_uia):
    element_id = query.find(name="Text Editor")["element"]["id"]
    result = query.get_prop(element_id, "not_a_prop")
    assert result["ok"] is False
    assert result["error"] == "UNKNOWN_PROP"
    assert "name" in result["available"]


def test_get_prop_unknown_id(fake_uia):
    result = query.get_prop("ghost", "name")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"


# --------------------------------------------------------------------------
# get_patterns
# --------------------------------------------------------------------------


def test_get_patterns_lists_supported(fake_uia):
    element_id = query.find(name="Text Editor")["element"]["id"]
    result = query.get_patterns(element_id)
    assert result["ok"] is True, result
    assert result["patterns"] == ["text", "value"]


def test_get_patterns_empty_when_none(fake_uia):
    element_id = query.find(name="Pane")["element"]["id"]
    assert query.get_patterns(element_id)["patterns"] == []


def test_get_patterns_unknown_id(fake_uia):
    result = query.get_patterns("ghost")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"


# --------------------------------------------------------------------------
# uiautomation import safety + tool registry
# --------------------------------------------------------------------------


def test_handlers_return_unavailable_without_library(monkeypatch):
    monkeypatch.setattr(query, "_uia", None)
    assert query.find(name="x")["error"] == "UNAVAILABLE"
    assert query.dump_tree("id")["error"] == "UNAVAILABLE"
    assert query.get_prop("id", "name")["error"] == "UNAVAILABLE"
    assert query.get_patterns("id")["error"] == "UNAVAILABLE"


def test_tools_list_contract():
    names = {t["name"] for t in query.TOOLS}
    assert names == {"find", "dump_tree", "get_prop", "get_patterns"}
    for tool in query.TOOLS:
        assert tool["description"]
        assert tool["handler"] is not None
        assert tool["schema"] is not None
        assert hasattr(tool["schema"], "model_json_schema")  # Pydantic v2


def test_handlers_accept_mapping_params(fake_uia):
    """Registry layer may call handler(params) or handler(**params)."""
    result = query.find({"name": "Text Editor"})
    assert result["ok"] is True
    result2 = query.find(name="Text Editor")
    assert result2["ok"] is True
    assert result["element"]["id"] == result2["element"]["id"]
