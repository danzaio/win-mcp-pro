"""Tests for the documented pack (post-P0).

``form_fill`` / ``table_extract`` / ``choose_file`` (tools.values),
``displays`` (tools.vision) and ``win_close`` (tools.winproc). Every seam is swappable:
fake UIA controls, fake display enumeration, real Pillow on tmp files, and
a fake user32/process layer - no screen, no real input, no editor launch.
"""

from __future__ import annotations

import pytest

from win_mcp_pro.tools import query, values, vision, winproc


# --------------------------------------------------------------------------
# fake UIA controls (same vocabulary as tests/test_values.py)


class FakePattern:
    def __init__(self, owner, kind):
        self._owner = owner
        self._kind = kind

    @property
    def Value(self):
        return self._owner._value

    def SetValue(self, text):
        if self._owner.read_only:
            raise RuntimeError("read-only")
        self._owner.set_calls.append(text)
        self._owner._value = text

    def Invoke(self):
        self._owner.invoke_count += 1


class FakeControl:
    def __init__(self, name="", control_type="", runtime_id=(), patterns=(),
                 children=None, value="", read_only=False):
        self.Name = name
        self.ControlTypeName = control_type
        self.RuntimeId = tuple(runtime_id)
        self._children = list(children or [])
        self._patterns = set(patterns)
        self._value = value
        self.read_only = read_only
        self.set_calls = []
        self.invoke_count = 0

    def GetChildren(self):
        return list(self._children)

    def GetValuePattern(self):
        return FakePattern(self, "value") if "value" in self._patterns else None

    def GetInvokePattern(self):
        return FakePattern(self, "invoke") if "invoke" in self._patterns else None


class FakeUia:
    def __init__(self, root):
        self._root = root

    def GetRootControl(self):
        return self._root


def install(monkeypatch, root):
    fake = FakeUia(root)
    monkeypatch.setattr(query, "_uia", fake)
    monkeypatch.setattr(values, "_uia", fake)
    query._registry_clear()


def reg(control, path="W", window="W"):
    return query._register(control, path, window).id


# --------------------------------------------------------------------------
# form_fill


def _form_tree():
    first = FakeControl("First", "EditControl", (11,), patterns={"value"}, value="")
    last = FakeControl("Last", "EditControl", (12,), patterns={"value"}, value="")
    locked = FakeControl("Locked", "EditControl", (13,), patterns={"value"},
                         value="old", read_only=True)
    return first, last, locked


def test_form_fill_writes_every_field_with_readback(monkeypatch):
    first, last, _locked = _form_tree()
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.form_fill(
        fields=[{"id": reg(first), "text": "Ada"},
                {"id": reg(last), "text": "Lovelace"}])
    assert result["ok"] is True, result
    assert [f["verified"] for f in result["fields"]] == [True, True]
    assert (first._value, last._value) == ("Ada", "Lovelace")


def test_form_fill_stops_on_error_by_default(monkeypatch):
    first, _last, locked = _form_tree()
    third = FakeControl("Third", "EditControl", (14,), patterns={"value"}, value="")
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.form_fill(
        fields=[{"id": reg(first), "text": "x"},
                {"id": reg(locked), "text": "y"},
                {"id": reg(third), "text": "z"}])
    assert result["ok"] is False, result
    assert third._value == "", "fields after the failure must stay untouched"
    assert result["fields"][1]["ok"] is False


def test_form_fill_continues_when_asked(monkeypatch):
    _first, _last, locked = _form_tree()
    third = FakeControl("Third", "EditControl", (14,), patterns={"value"}, value="")
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.form_fill(
        fields=[{"id": reg(locked), "text": "y"},
                {"id": reg(third), "text": "z"}],
        stop_on_error=False)
    assert result["ok"] is False and third._value == "z", result


def test_form_fill_unknown_id_is_typed(monkeypatch):
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.form_fill(fields=[{"id": "nope", "text": "x"}])
    assert result["ok"] is False, result
    assert result["fields"][0]["error"] == "NOT_FOUND", result


# --------------------------------------------------------------------------
# table_extract


def _grid(rows=3, cols=2):
    cells = []
    for row in range(1, rows + 1):
        items = [FakeControl(f"R{row}C{col}", "CellControl", (row * 10 + col,))
                 for col in range(1, cols + 1)]
        cells.append(FakeControl(f"row{row}", "RowControl", (100 + row,), children=items))
    return FakeControl("grid", "TableControl", (999,), children=cells)


def test_table_extract_reads_rows_and_cells(monkeypatch):
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.table_extract(id=reg(_grid()))
    assert result["ok"] is True, result
    assert result["shape"] == [3, 2], result
    assert result["rows"][1] == ["R2C1", "R2C2"], result


def test_table_extract_caps_giant_grids(monkeypatch):
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.table_extract(id=reg(_grid(rows=205, cols=2)))
    assert result["ok"] is True, result
    assert result["shape"][0] == 200 and result["truncated"] is True, result


def test_table_extract_without_rows_is_typed(monkeypatch):
    install(monkeypatch, FakeControl("root", "PaneControl"))
    button = FakeControl("OK", "ButtonControl", (9,), patterns={"invoke"})
    result = values.table_extract(id=reg(button))
    assert result["ok"] is False and result["error"] == "TABLE_EMPTY", result


def test_table_extract_winform_shape_with_header(monkeypatch):
    """WinForms DataGridView: Custom rows, Header/DataItem cells, content in
    the value (not the Name). Cells matching as rows contribute nothing."""
    header = FakeControl("Linha Superior", "CustomControl", (50,), children=[
        FakeControl("Key", "HeaderControl", (51,)),
        FakeControl("Val", "HeaderControl", (52,))])
    rows = []
    for number, (key, val) in enumerate((("alpha", "1"), ("beta", "2"))):
        rows.append(FakeControl(f"Linha {number}", "CustomControl", (60 + number,), children=[
            FakeControl(f"Key Linha {number}", "DataItemControl", (70 + number * 2,),
                        patterns={"value"}, value=key),
            FakeControl(f"Val Linha {number}", "DataItemControl", (71 + number * 2,),
                        patterns={"value"}, value=val)]))
    grid = FakeControl("grid", "TableControl", (999,), children=[header, *rows])
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.table_extract(id=reg(grid))
    assert result["ok"] is True, result
    assert result["rows"] == [["Key", "Val"], ["alpha", "1"], ["beta", "2"]], result
    assert result["shape"] == [3, 2], result


# --------------------------------------------------------------------------
# choose_file


def _dialog(confirm="Open"):
    field = FakeControl("File name:", "ComboBoxControl", (21,))
    edit = FakeControl("", "EditControl", (22,), patterns={"value"}, value="")
    field._children.append(edit)
    confirm_button = FakeControl(confirm, "ButtonControl", (23,), patterns={"invoke"})
    return FakeControl(confirm, "WindowControl", (20,),
                       children=[field, confirm_button]), edit, confirm_button


def test_choose_file_types_path_and_confirms(monkeypatch, tmp_path):
    dialog, edit, button = _dialog()
    install(monkeypatch, FakeControl("root", "PaneControl"))
    target = tmp_path / "note.txt"
    target.write_text("hi", encoding="utf-8")
    result = values.choose_file(dialog=reg(dialog), path=str(target))
    assert result["ok"] is True, result
    assert edit._value == str(target) and button.invoke_count == 1, result


def test_choose_file_refuses_a_missing_file_without_touching(monkeypatch, tmp_path):
    dialog, edit, button = _dialog()
    install(monkeypatch, FakeControl("root", "PaneControl"))
    result = values.choose_file(dialog=reg(dialog),
                                path=str(tmp_path / "ghost.txt"))
    assert result["ok"] is False and result["error"] == "PATH_MISSING", result
    assert edit._value == "" and button.invoke_count == 0


def test_choose_file_save_accepts_a_new_name_in_an_existing_dir(monkeypatch, tmp_path):
    dialog, edit, button = _dialog("Save")
    install(monkeypatch, FakeControl("root", "PaneControl"))
    fresh = tmp_path / "fresh.txt"
    result = values.choose_file(dialog=reg(dialog), path=str(fresh), mode="save")
    assert result["ok"] is True, result
    assert edit._value == str(fresh) and button.invoke_count == 1


def test_choose_file_without_a_filename_field_is_typed(monkeypatch, tmp_path):
    dialog = FakeControl("Open", "WindowControl", (20,), children=[
        FakeControl("Open", "ButtonControl", (23,), patterns={"invoke"})])
    install(monkeypatch, FakeControl("root", "PaneControl"))
    target = tmp_path / "note.txt"
    target.write_text("hi", encoding="utf-8")
    result = values.choose_file(dialog=reg(dialog), path=str(target))
    assert result["ok"] is False and result["error"] == "FIELD_NOT_FOUND", result


# --------------------------------------------------------------------------
# displays


def test_displays_lists_monitors(monkeypatch):
    monkeypatch.setattr(
        vision, "_enum_displays",
        lambda: [{"index": 0, "name": "\\\\.\\DISPLAY1", "left": 0, "top": 0,
                  "width": 1920, "height": 1080, "primary": True,
                  "dpi": [96, 96]},
                 {"index": 1, "name": "\\\\.\\DISPLAY2", "left": 1920, "top": 0,
                  "width": 1920, "height": 1080, "primary": False,
                  "dpi": [96, 96]}])
    result = vision.handler_displays({})
    assert result["ok"] is True, result
    assert result["count"] == 2
    assert sum(1 for d in result["displays"] if d["primary"]) == 1


def test_displays_failure_is_typed(monkeypatch):
    def boom():
        raise OSError("EnumDisplayMonitors failed")

    monkeypatch.setattr(vision, "_enum_displays", boom)
    result = vision.handler_displays({})
    assert result["ok"] is False and result["error"] == "DISPLAY_UNREADABLE", result


# --------------------------------------------------------------------------
# img_convert


def _png(path, w=120, h=80):
    from PIL import Image

    Image.new("RGB", (w, h), "white").save(path)


# --------------------------------------------------------------------------
# win_close


def _call_winproc(name, args):
    for tool in winproc.TOOLS:
        if tool["name"] == name:
            return tool["handler"](args)
    raise AssertionError(f"tool not registered: {name}")


def test_win_close_posts_close_and_confirms_gone(monkeypatch):
    seen = []
    states = {"alive": True}

    class FakeUser32:
        def IsWindow(self, hwnd):
            return states["alive"]

        def PostMessageW(self, hwnd, msg, w, l):
            seen.append((hwnd, msg))
            states["alive"] = False
            return True

    monkeypatch.setattr(winproc, "_user32", FakeUser32())
    result = _call_winproc("win_close", {"hwnd": 4321})
    assert result == {"ok": True, "hwnd": 4321}
    assert seen == [(4321, 0x0010)]


def test_win_close_unknown_hwnd_is_typed(monkeypatch):
    class FakeUser32:
        def IsWindow(self, hwnd):
            return False

    monkeypatch.setattr(winproc, "_user32", FakeUser32())
    with pytest.raises(winproc.WinProcError) as exc:
        _call_winproc("win_close", {"hwnd": 999})
    assert exc.value.code == "NOT_FOUND"


def test_win_close_that_ignores_close_is_typed(monkeypatch):
    class FakeUser32:
        def IsWindow(self, hwnd):
            return True

        def PostMessageW(self, hwnd, msg, w, l):
            return True

    monkeypatch.setattr(winproc, "_user32", FakeUser32())
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return 1000.0 + ticks["n"] * 10.0

    monkeypatch.setattr(winproc.time, "monotonic", clock)
    with pytest.raises(winproc.WinProcError) as exc:
        _call_winproc("win_close", {"hwnd": 4321})
    assert exc.value.code == "CLOSE_UNCONFIRMED"


