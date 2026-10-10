"""Tests for tools.values.

Everything is mocked: a fake UIA backend replaces the uiautomation module and
fake controls duck-type the small Control surface values.py uses. No Windows
desktop and no uiautomation package required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import values
from tools.values import (
    ClipboardUnavailableError,
    ElementNotFoundError,
    InvalidElementIdError,
    MenuPathError,
    PatternMissingError,
    UiaUnavailableError,
    ValueReadOnlyError,
)

# UIA ControlTypeIds (standard, stable across Windows versions)
CT_WINDOW = 50032
CT_PANE = 50033
CT_EDIT = 50004
CT_TEXT = 50020
CT_BUTTON = 50000
CT_MENUBAR = 50010
CT_MENUITEM = 50011

HWND = 1234


class FakeValuePattern:
    def __init__(self, control: "FakeControl", value: str) -> None:
        self._control = control
        self.Value = value

    def SetValue(self, text: str) -> None:
        if self._control._read_only:
            raise RuntimeError("provider refused SetValue on read-only control")
        self._control.set_calls.append(text)
        self._control._value = text
        self.Value = text


class FakeInvokePattern:
    def __init__(self, control: "FakeControl") -> None:
        self._control = control

    def Invoke(self) -> None:
        self._control.invoke_count += 1
        if self._control._reveal_on_invoke:
            self._control.reveal_submenu()


class FakeExpandCollapsePattern:
    def __init__(self, control: "FakeControl") -> None:
        self._control = control

    def Expand(self) -> None:
        self._control.expand_count += 1
        self._control.reveal_submenu()

    def Collapse(self) -> None:
        pass


class FakeControl:
    """Duck-types the uiautomation Control surface values.py touches."""

    def __init__(
        self,
        name: str = "",
        control_type: int = 0,
        runtime_id: tuple[int, ...] = (),
        *,
        supports_value: bool = False,
        read_only: bool = False,
        invokable: bool = False,
        expandable: bool = False,
        reveal_on_invoke: bool = False,
        initial_value: str = "",
    ) -> None:
        self.Name = name
        self.ControlType = control_type
        self.RuntimeId = tuple(runtime_id)
        self._supports_value = supports_value
        self._read_only = read_only
        self._invokable = invokable
        self._expandable = expandable
        self._reveal_on_invoke = reveal_on_invoke
        self._value = initial_value
        self._children: list[FakeControl] = []
        self._submenu: list[FakeControl] = []
        self._parent: FakeControl | None = None
        self.focused = 0
        self.set_calls: list[str] = []
        self.invoke_count = 0
        self.expand_count = 0

    # tree building
    def add_child(self, child: "FakeControl") -> "FakeControl":
        child._parent = self
        self._children.append(child)
        return self

    def add_submenu_item(self, child: "FakeControl") -> "FakeControl":
        self._submenu.append(child)
        return self

    def reveal_submenu(self) -> None:
        for item in self._submenu:
            if item not in self._children:
                self.add_child(item)

    # navigation (first-child / next-sibling, like the real library)
    def GetFirstChildControl(self) -> "FakeControl | None":
        return self._children[0] if self._children else None

    def GetNextSiblingControl(self) -> "FakeControl | None":
        if self._parent is None:
            return None
        siblings = self._parent._children
        index = siblings.index(self)
        return siblings[index + 1] if index + 1 < len(siblings) else None

    # patterns
    def GetValuePattern(self) -> FakeValuePattern | None:
        if not self._supports_value:
            return None
        return FakeValuePattern(self, self._value)

    def GetInvokePattern(self) -> FakeInvokePattern | None:
        return FakeInvokePattern(self) if self._invokable else None

    def GetExpandCollapsePattern(self) -> FakeExpandCollapsePattern | None:
        return FakeExpandCollapsePattern(self) if self._expandable else None

    def SetFocus(self) -> None:
        self.focused += 1


class FakeBackend:
    """Stands in for the uiautomation module itself."""

    def __init__(self, root: FakeControl) -> None:
        self._root = root
        self.ControlFromHandle_calls: list[int] = []
        self.foreground = root
        self.SetClipboardText_calls: list[str] = []
        self.SendKeys_calls: list[str] = []
        self._clipboard = "OLD"

    def ControlFromHandle(self, hwnd: int) -> FakeControl:
        self.ControlFromHandle_calls.append(hwnd)
        return self._root

    def GetForegroundWindow(self) -> FakeControl:
        return self.foreground

    def GetClipboardText(self) -> str:
        return self._clipboard

    def SetClipboardText(self, text: str) -> None:
        self.SetClipboardText_calls.append(text)
        self._clipboard = text

    def SendKeys(self, keys: str) -> None:
        self.SendKeys_calls.append(keys)


@pytest.fixture
def desktop(monkeypatch):
    edit = FakeControl("Editor", CT_EDIT, (42,), supports_value=True, initial_value="")
    readonly_text = FakeControl("Static label", CT_TEXT, (43,))
    locked = FakeControl("Locked field", CT_EDIT, (44,), supports_value=True, read_only=True, initial_value="old")
    ok_button = FakeControl("OK", CT_BUTTON, (60,), invokable=True)

    panel = FakeControl("Client Area", CT_PANE, (7,))
    for child in (edit, readonly_text, locked, ok_button):
        panel.add_child(child)

    submenu_labels = {"New": 50, "Open": 51, "Save": 52, "Save As": 53, "Exit": 54}
    file_item = FakeControl(
        "File",
        CT_MENUITEM,
        (31,),
        invokable=True,
        expandable=True,
        reveal_on_invoke=True,
    )
    for label, runtime_id in submenu_labels.items():
        file_item.add_submenu_item(FakeControl(label, CT_MENUITEM, (runtime_id,), invokable=True))
    menubar = FakeControl("Menu Bar", CT_MENUBAR, (30,))
    menubar.add_child(file_item)

    window = FakeControl("Fake App", CT_WINDOW, (1,), invokable=True)
    window.add_child(menubar)
    window.add_child(panel)

    backend = FakeBackend(window)
    monkeypatch.setattr(values, "_uia", backend)

    return SimpleNamespace(
        backend=backend,
        window=window,
        edit=edit,
        readonly_text=readonly_text,
        locked=locked,
        ok_button=ok_button,
        menubar=menubar,
        file_item=file_item,
        save_as=file_item._submenu[3],
    )


# --- value patterns ---------------------------------------------------------


def test_set_get_roundtrip_shape(desktop):
    result = values.set_value(f"{HWND}:42", "hello world")
    assert result == {"ok": True, "before": "", "after": "hello world"}
    # one atomic ValuePattern write, never per-character input
    assert desktop.edit.set_calls == ["hello world"]
    assert desktop.edit.focused == 0
    assert desktop.backend.SendKeys_calls == []
    assert desktop.backend.ControlFromHandle_calls == [HWND]

    assert values.get_value(f"{HWND}:42") == {"ok": True, "value": "hello world"}


def test_set_value_missing_pattern_raises_typed_error(desktop):
    with pytest.raises(PatternMissingError) as err:
        values.set_value(f"{HWND}:43", "x")
    assert err.value.code == "PATTERN_MISSING"
    assert "ValuePattern" in str(err.value)

    with pytest.raises(PatternMissingError):
        values.set_value(f"{HWND}:60", "x")


def test_set_value_read_only_pattern_raises_typed_error(desktop):
    with pytest.raises(ValueReadOnlyError) as err:
        values.set_value(f"{HWND}:44", "x")
    assert err.value.code == "VALUE_READ_ONLY"
    assert desktop.locked.set_calls == []


def test_get_value_missing_pattern_raises_typed_error(desktop):
    with pytest.raises(PatternMissingError) as err:
        values.get_value(f"{HWND}:60")
    assert err.value.code == "PATTERN_MISSING"


def test_invalid_element_id_raises(desktop):
    with pytest.raises(InvalidElementIdError) as err:
        values.get_value("not-an-id")
    assert err.value.code == "INVALID_ELEMENT_ID"


def test_unknown_element_id_raises_not_found(desktop):
    with pytest.raises(ElementNotFoundError) as err:
        values.set_value(f"{HWND}:999", "x")
    assert err.value.code == "NOT_FOUND"


def test_resolve_element_stops_on_cyclic_tree(desktop):
    loop = FakeControl("Loop", CT_PANE, (0,))
    original = loop.GetFirstChildControl

    def self_child():
        return loop

    loop.GetFirstChildControl = self_child  # type: ignore[method-assign]
    desktop.window.add_child(loop)

    with pytest.raises(ElementNotFoundError) as err:
        values.resolve_element(f"{HWND}:12345", max_nodes=100)
    assert err.value.code == "NOT_FOUND"
    loop.GetFirstChildControl = original


def test_handlers_raise_typed_error_when_backend_missing(monkeypatch):
    monkeypatch.setattr(values, "_uia", None)
    with pytest.raises(UiaUnavailableError) as err:
        values.set_value(f"{HWND}:42", "x")
    assert err.value.code == "UIAUTOMATION_UNAVAILABLE"
    with pytest.raises(UiaUnavailableError):
        values.menu_click(["File", "Save As"])


# --- clipboard paste --------------------------------------------------------


def test_paste_text_uses_clipboard_and_restores(desktop):
    long_text = "lorem ipsum dolor sit amet " * 25
    result = values.paste_text(f"{HWND}:42", long_text)
    assert result == {"ok": True, "chars": len(long_text), "clipboard_restored": True}
    assert desktop.edit.focused == 1
    assert desktop.backend.SetClipboardText_calls == [long_text, "OLD"]
    assert desktop.backend.SendKeys_calls == ["{Ctrl}v"]
    # paste never writes the control through ValuePattern
    assert desktop.edit.set_calls == []


def test_paste_text_restores_clipboard_when_send_fails(desktop, monkeypatch):
    def boom(keys: str) -> None:
        raise RuntimeError("input blocked")

    monkeypatch.setattr(desktop.backend, "SendKeys", boom)
    with pytest.raises(RuntimeError):
        values.paste_text(f"{HWND}:42", "text")
    assert desktop.backend._clipboard == "OLD"


def test_paste_text_without_clipboard_backend_raises(desktop, monkeypatch):
    monkeypatch.setattr(desktop.backend, "SetClipboardText", None)
    with pytest.raises(ClipboardUnavailableError) as err:
        values.paste_text(f"{HWND}:42", "text")
    assert err.value.code == "CLIPBOARD_UNAVAILABLE"


# --- menu navigation --------------------------------------------------------


def test_menu_click_resolves_path_structurally(desktop):
    # submenu items are not visible until File is expanded
    assert [c.Name for c in desktop.file_item._children] == []
    result = values.menu_click(["File", "Save As"])
    assert result == {"ok": True, "path": ["File", "Save As"], "clicked": "Save As"}
    assert desktop.file_item.expand_count == 1
    assert desktop.save_as.invoke_count == 1
    assert desktop.save_as.focused == 0


def test_menu_click_normalizes_accelerators_and_case(desktop):
    result = values.menu_click(["&file", "save as"])
    assert result["clicked"] == "Save As"


def test_menu_click_leaf_item(desktop):
    result = values.menu_click(["File"])
    assert result == {"ok": True, "path": ["File"], "clicked": "File"}


def test_menu_click_missing_item_raises_typed_error(desktop):
    with pytest.raises(MenuPathError) as err:
        values.menu_click(["File", "Nope"])
    assert err.value.code == "MENU_PATH_NOT_FOUND"
    assert "File" in str(err.value)
    assert "Nope" in str(err.value)

    with pytest.raises(MenuPathError):
        values.menu_click(["Missing Top Level"])
    assert desktop.file_item.expand_count + desktop.file_item.invoke_count == 0


# --- registry contract ------------------------------------------------------


def test_tools_registry_contract():
    names = [tool.name for tool in values.TOOLS]
    assert names == ["set_value", "get_value", "paste_text", "menu_click"]
    for tool in values.TOOLS:
        assert tool.description and len(tool.description) > 20
        assert isinstance(tool.schema, type) and issubclass(tool.schema, BaseModel)
        assert callable(tool.handler)
        schema = tool.schema.model_json_schema()
        assert schema.get("type") == "object"
        for field in schema.get("required", []):
            assert field in schema["properties"]
    # dict-style interop for registry integration
    first = values.TOOLS[0]
    assert first["name"] == "set_value"
    assert dict(first)["schema"] is values.SetValueArgs


def test_handler_accepts_schema_instance_or_mapping(desktop):
    tool = next(t for t in values.TOOLS if t.name == "set_value")
    result = tool.handler(values.SetValueArgs(id=f"{HWND}:42", text="one"))
    assert result["after"] == "one"
    result = tool.handler({"id": f"{HWND}:42", "text": "two"})
    assert result["after"] == "two"


def test_menu_click_schema_validates_path():
    with pytest.raises(ValidationError):
        values.MenuClickArgs(path=[])
    with pytest.raises(ValidationError):
        values.MenuClickArgs(path=["File", "   "])
    args = values.MenuClickArgs(path=["File", "Save As"])
    assert args.path == ["File", "Save As"]


def test_set_value_schema_requires_id_and_text():
    with pytest.raises(ValidationError):
        values.SetValueArgs(text="orphan")
    with pytest.raises(ValidationError):
        values.SetValueArgs(id=f"{HWND}:42")
