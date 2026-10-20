"""Tests for tools/values.py — set_value / get_value / paste_text / menu_click.

Mirrors the conventions of tools/query.py (the act layer's sibling):

- The UIA layer is a fake shipped inline (no Windows, no uiautomation).
- Element ids resolve through query's in-process registry first, and through
  the literal "hwnd:runtimeid" form as a fallback (snapshot-style ids).
- Handlers return ``{"ok": True, ...}`` or ``{"ok": False, "error": CODE, ...}``
  and accept either a mapping or keyword arguments.
- Clipboard and keyboard input funnel through tools/compat.py seams, which are
  swapped out here, so no test touches a real desktop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tools import compat, query, values


# --------------------------------------------------------------------------
# fake UIA tree
# --------------------------------------------------------------------------


class FakePattern:
    """Stand-in for a UIA pattern object; records every call."""

    def __init__(self, owner: FakeControl, kind: str) -> None:
        self._owner = owner
        self._kind = kind

    @property
    def Value(self) -> str:
        return self._owner._value

    def SetValue(self, text: str) -> None:
        if self._owner.read_only:
            raise RuntimeError("control refused SetValue (read-only)")
        self._owner.set_calls.append(text)
        self._owner._value = text

    def Invoke(self) -> None:
        self._owner.invoke_count += 1
        self._owner.reveal_submenu()

    def Expand(self) -> None:
        self._owner.expand_count += 1
        self._owner.reveal_submenu()

    def Collapse(self) -> None:
        self._owner.collapse_count += 1


class FakeControl:
    """Minimal stand-in for uiautomation.Control (children + patterns)."""

    def __init__(self, name="", control_type="", runtime_id=(), patterns=(),
                 children=None, value="", read_only=False, handle=0):
        self.Name = name
        self.ControlTypeName = control_type
        self.RuntimeId = tuple(runtime_id)
        self.NativeWindowHandle = handle
        self.hops = []
        self._children = list(children or [])
        self._patterns = set(patterns)
        self._value = value
        self.read_only = read_only
        self.set_calls: list[str] = []
        self.invoke_count = 0
        self.expand_count = 0
        self.collapse_count = 0
        self.focused = 0
        self._submenu: list[FakeControl] = []
        self._reveal_container: FakeControl | None = None

    def GetChildren(self):
        self.hops.append(self.Name)
        return list(self._children)

    def _probe(self, pattern):
        return FakePattern(self, pattern) if pattern in self._patterns else None

    def GetValuePattern(self):
        return self._probe("value")

    def GetInvokePattern(self):
        return self._probe("invoke")

    def GetExpandCollapsePattern(self):
        return self._probe("expand_collapse")

    def SetFocus(self):
        self.focused += 1

    # menu helpers -------------------------------------------------------
    def add_submenu(self, items, reveal_container=None):
        """Queue submenu items; they appear when the item is expanded/invoked.

        ``reveal_container`` mimics providers that parent the popup menu to
        the window instead of to the menu item itself.
        """
        self._submenu = items
        self._reveal_container = reveal_container or self
        return self

    def reveal_submenu(self):
        if self._reveal_container is None:
            return
        for item in self._submenu:
            if item not in self._reveal_container._children:
                self._reveal_container._children.append(item)


class FakeUia:
    """Fake uiautomation module (root + foreground window + handle lookup)."""

    def __init__(self, root):
        self._root = root

    def GetRootControl(self):
        return self._root

    def GetForegroundWindow(self):
        return self._root._children[0]

    def ControlFromHandle(self, hwnd):
        for child in self._root._children:
            if getattr(child, "NativeWindowHandle", 0) == hwnd:
                return child
        return None


def build_menu_items():
    labels = {"New": 50, "Open": 51, "Save": 52, "Save As": 53, "Exit": 54}
    return [
        FakeControl(label, "MenuItemControl", (rid,), patterns={"invoke"})
        for label, rid in labels.items()
    ]


def build_tree(submenu_reveals_under="item"):
    editor = FakeControl("Text Editor", "EditControl", (42,),
                         patterns={"value", "text"}, value="")
    hint = FakeControl("Static hint", "TextControl", (43,), patterns={"text"})
    locked = FakeControl("Locked field", "EditControl", (44,),
                         patterns={"value"}, value="old", read_only=True)
    ok_button = FakeControl("OK", "ButtonControl", (60,), patterns={"invoke"})

    pane = FakeControl("Client", "PaneControl", children=[editor, hint, locked, ok_button])

    file_item = FakeControl("File", "MenuItemControl", (31,),
                            patterns={"invoke", "expand_collapse"})
    menubar = FakeControl("Application", "MenuBarControl", (30,), children=[file_item])

    window = FakeControl("Fake App", "WindowControl", (1,), handle=1234,
                         children=[menubar, pane])
    root = FakeControl("Desktop", "PaneControl", children=[window])

    reveal_container = file_item if submenu_reveals_under == "item" else window
    file_item.add_submenu(build_menu_items(), reveal_container)
    return SimpleNamespace(
        root=root, window=window, menubar=menubar, file_item=file_item,
        submenu=file_item._submenu, save_as=file_item._submenu[3],
        pane=pane, editor=editor, hint=hint, locked=locked, ok_button=ok_button,
    )


def install_tree(monkeypatch, parts):
    fake = FakeUia(parts.root)
    monkeypatch.setattr(query, "_uia", fake)
    monkeypatch.setattr(values, "_uia", fake)
    query._registry_clear()
    editor_id = query._register(parts.editor, "Window:Fake App > Client", "Fake App").id
    locked_id = query._register(parts.locked, "Window:Fake App > Client", "Fake App").id
    hint_id = query._register(parts.hint, "Window:Fake App > Client", "Fake App").id
    button_id = query._register(parts.ok_button, "Window:Fake App > Client", "Fake App").id
    return SimpleNamespace(editor=editor_id, locked=locked_id, hint=hint_id, button=button_id)


@pytest.fixture()
def fake_uia(monkeypatch):
    parts = build_tree(submenu_reveals_under="item")
    ids = install_tree(monkeypatch, parts)
    return SimpleNamespace(**vars(parts), ids=ids)


# --------------------------------------------------------------------------
# value patterns
# --------------------------------------------------------------------------


def test_set_get_roundtrip_shape(fake_uia):
    ids = fake_uia.ids
    result = values.set_value(ids.editor, "hello world")
    assert result == {
        "ok": True,
        "id": ids.editor,
        "before": "",
        "after": "hello world",
        "verified": True,
    }
    # one atomic ValuePattern write, never per-character input
    assert fake_uia.editor.set_calls == ["hello world"]
    assert fake_uia.editor.focused == 0  # value writes never focus the control

    get = values.get_value(ids.editor)
    assert get == {"ok": True, "id": ids.editor, "value": "hello world"}


def test_set_value_resolves_hwnd_runtimeid_form(fake_uia):
    """Snapshot-style ids ('hwnd:runtimeid') resolve without the registry."""
    result = values.set_value("1234:42", "typed")
    assert result["ok"] is True, result
    assert result["after"] == "typed"
    assert values.get_value("1234:42")["value"] == "typed"


def test_set_value_missing_pattern_is_typed_error(fake_uia):
    for target in (fake_uia.ids.hint, fake_uia.ids.button):
        result = values.set_value(target, "x")
        assert result["ok"] is False
        assert result["error"] == "PATTERN_MISSING"
        assert "ValuePattern" in result["reason"]


def test_set_value_read_only_refusal_is_typed_error(fake_uia):
    result = values.set_value(fake_uia.ids.locked, "x")
    assert result == {
        "ok": False,
        "error": "VALUE_READ_ONLY",
        "id": fake_uia.ids.locked,
    } or (result["error"] == "VALUE_READ_ONLY" and "refused" in result["reason"])
    assert fake_uia.locked.set_calls == []


def test_get_value_missing_pattern_is_typed_error(fake_uia):
    result = values.get_value(fake_uia.ids.button)
    assert result["ok"] is False
    assert result["error"] == "PATTERN_MISSING"
    assert result["reason"]


def test_unknown_id_is_typed_not_found(fake_uia):
    result = values.get_value("0123456789ab")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"
    assert result["reason"]
    assert "hwnd:runtimeid" in result["reason"]


def test_runtime_lookup_stops_on_cyclic_tree(monkeypatch):
    """A control whose children include itself must degrade, not hang."""
    parts = build_tree()
    loop = FakeControl("Loop", "PaneControl", (99,))
    original_children = parts.window._children

    def looping_children():
        return [loop]

    loop.GetChildren = looping_children  # type: ignore[method-assign]
    parts.window.GetChildren = looping_children  # type: ignore[method-assign]
    install_tree(monkeypatch, parts)
    try:
        # runtime 77 does not exist; the walk must still terminate
        result = values.set_value("1234:77", "x")
        assert result["ok"] is False
        assert result["error"] == "NOT_FOUND"
    finally:
        parts.window._children = original_children


def test_runtime_tuple_uses_get_runtime_id():
    """Real uiautomation controls expose GetRuntimeId() and no RuntimeId
    attribute; the walk must read the method (the live bench caught the
    attribute-only read, which made snapshot-derived ids unresolvable)."""

    class RealShape:
        def GetRuntimeId(self):
            return [42, 123]

    assert values._runtime_tuple(RealShape()) == (42, 123)


def test_runtime_tuple_falls_back_to_the_attribute():
    class Legacy:
        RuntimeId = [7, 8]

    assert values._runtime_tuple(Legacy()) == (7, 8)


def test_handlers_return_unavailable_without_library(monkeypatch):
    monkeypatch.setattr(query, "_uia", None)
    monkeypatch.setattr(values, "_uia", None)
    assert values.set_value("1234:42", "x")["error"] == "UNAVAILABLE"
    assert values.get_value("1234:42")["error"] == "UNAVAILABLE"
    assert values.paste_text("1234:42", "x")["error"] == "UNAVAILABLE"
    assert values.menu_click(["File"])["error"] == "UNAVAILABLE"


# --------------------------------------------------------------------------
# clipboard paste
# --------------------------------------------------------------------------


@pytest.fixture()
def clipboard(monkeypatch):
    """Swap compat's input funnels so no real input reaches the machine."""
    state = {"text": "OLD", "writes": [], "events": []}

    def read():
        if state["text"] is None:
            raise RuntimeError("no text on the clipboard")
        return state["text"]

    def write(text):
        state["writes"].append(text)
        state["text"] = text

    monkeypatch.setattr(compat, "_clipboard_read", read)
    monkeypatch.setattr(compat, "_clipboard_write", write)
    monkeypatch.setattr(compat, "_send_input", lambda events: state["events"].extend(events))
    return state


def test_paste_text_clipboard_roundtrip(fake_uia, clipboard):
    long_text = "lorem ipsum dolor sit amet " * 25
    result = values.paste_text(fake_uia.ids.editor, long_text)
    assert result == {
        "ok": True,
        "id": fake_uia.ids.editor,
        "chars": len(long_text),
        "clipboard_restored": True,
    }
    # focus lands before the keystroke, so the text reaches the field
    assert fake_uia.editor.focused == 1
    assert clipboard["writes"] == [long_text, "OLD"]
    assert clipboard["events"] == [
        {"type": "key", "vk": compat.VK["ctrl"], "up": False},
        {"type": "key", "vk": compat._vk("v"), "up": False},
        {"type": "key", "vk": compat._vk("v"), "up": True},
        {"type": "key", "vk": compat.VK["ctrl"], "up": True},
    ]
    # paste never writes the control through ValuePattern
    assert fake_uia.editor.set_calls == []


def test_paste_text_restores_clipboard_when_input_fails(fake_uia, clipboard, monkeypatch):
    def boom(events):
        raise OSError("SendInput rejected the events")

    monkeypatch.setattr(compat, "_send_input", boom)
    result = values.paste_text(fake_uia.ids.editor, "text")
    assert result["ok"] is False
    assert result["error"] == "INPUT_FAILED"
    assert "SendInput" in result["reason"]
    assert clipboard["text"] == "OLD"  # the caller's clipboard survives the failure


def test_paste_text_without_previous_clipboard(fake_uia, clipboard):
    clipboard["text"] = None
    result = values.paste_text(fake_uia.ids.editor, "text")
    assert result["ok"] is True, result
    assert result["clipboard_restored"] is False


def test_paste_text_rejects_oversized_budget(fake_uia, clipboard):
    result = values.paste_text(fake_uia.ids.editor, "x" * (compat.CLIPBOARD_TEXT_MAX + 1))
    assert result["ok"] is False
    assert result["error"] == "BAD_REQUEST"


# --------------------------------------------------------------------------
# menu navigation
# --------------------------------------------------------------------------


def test_menu_click_resolves_path_structurally(fake_uia):
    # submenu items are invisible until File is expanded
    assert [c.Name for c in fake_uia.file_item._children] == []
    result = values.menu_click(["File", "Save As"])
    assert result == {
        "ok": True,
        "path": ["File", "Save As"],
        "clicked": "Save As",
        "expanded": ["File"],
    }
    assert fake_uia.file_item.expand_count == 1
    assert fake_uia.save_as.invoke_count == 1


def test_menu_click_finds_submenu_parented_to_window(monkeypatch):
    """Providers that parent the popup to the window still resolve."""
    parts = build_tree(submenu_reveals_under="window")
    install_tree(monkeypatch, parts)
    result = values.menu_click(["File", "Save As"])
    assert result["ok"] is True, result
    assert result["clicked"] == "Save As"

def test_menu_click_uses_the_foreground_window_not_the_first_desktop_child(monkeypatch):
    """With another window first in Z-order, the menu must come from the foreground one."""
    parts = build_tree()
    decoy = FakeControl("Decoy", "WindowControl", (9,), handle=999, children=[])
    parts.root._children.insert(0, decoy)
    install_tree(monkeypatch, parts)
    real_window = parts.root._children[1]
    monkeypatch.setattr(
        values._uia, "GetForegroundControl", lambda: real_window, raising=False
    )
    result = values.menu_click(["File", "Save As"])
    assert result["ok"] is True, result
    assert result["clicked"] == "Save As"


def test_menu_click_normalizes_accelerators_and_case(fake_uia):
    result = values.menu_click(["&file", "save as"])
    assert result["ok"] is True
    assert result["clicked"] == "Save As"


def test_menu_click_leaf_item(fake_uia):
    result = values.menu_click(["File"])
    assert result == {"ok": True, "path": ["File"], "clicked": "File", "expanded": []}


def test_menu_click_missing_item_is_typed_error(fake_uia):
    result = values.menu_click(["File", "Nope"])
    assert result["ok"] is False
    assert result["error"] == "MENU_PATH_NOT_FOUND"
    assert "File > Nope" in result["reason"]


def test_menu_click_missing_top_level_leaves_menu_untouched(fake_uia):
    result = values.menu_click(["Missing Top Level"])
    assert result["error"] == "MENU_PATH_NOT_FOUND"
    assert fake_uia.file_item.expand_count == 0
    assert fake_uia.file_item.invoke_count == 0


def test_menu_click_rejects_empty_and_blank_paths(fake_uia):
    assert values.menu_click([])["error"] == "BAD_REQUEST"
    assert values.menu_click(["File", "   "])["error"] == "BAD_REQUEST"


def test_menu_click_rejects_absurdly_long_path(fake_uia):
    assert values.menu_click([f"m{i}" for i in range(13)])["error"] == "BAD_REQUEST"


# --------------------------------------------------------------------------
# schema + registry contract
# --------------------------------------------------------------------------


def test_schemas_validate():
    with pytest.raises(ValidationError):
        values.SetValueArgs(id="x")
    with pytest.raises(ValidationError):
        values.MenuClickArgs(path=[])
    with pytest.raises(ValidationError):
        values.MenuClickArgs(path=["File", " "])
    args = values.MenuClickArgs(path=["File", "Save As"])
    assert args.path == ["File", "Save As"]
    assert values.SetValueArgs(id="x", text="y").text == "y"


def test_tools_list_contract():
    names = [t["name"] for t in values.TOOLS]
    assert names == ["set_value", "get_value", "paste_text", "menu_click",
                      "form_fill", "table_extract", "choose_file"]
    for tool in values.TOOLS:
        assert tool["description"] and len(tool["description"]) > 20
        assert tool["handler"] is not None
        assert hasattr(tool["schema"], "model_json_schema")  # Pydantic v2
        assert tool["schema"].model_json_schema().get("type") == "object"
        for field in tool["schema"].model_json_schema().get("required", []):
            assert field in tool["schema"].model_json_schema()["properties"]


def test_handlers_accept_mapping_or_kwargs(fake_uia):
    """Registry layer may call handler(params) or handler(**params)."""
    set_tool = values.TOOLS[0]
    by_mapping = set_tool["handler"]({"id": fake_uia.ids.editor, "text": "one"})
    assert by_mapping["ok"] is True
    by_kwargs = set_tool["handler"](id=fake_uia.ids.editor, text="two")
    assert by_kwargs["ok"] is True
    assert by_kwargs["after"] == "two"


def test_schemas_reject_unknown_fields():
    with pytest.raises(ValidationError):
        values.SetValueArgs(id="x", text="y", nonsense=True)
