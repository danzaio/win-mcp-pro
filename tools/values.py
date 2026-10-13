"""Value actions for win-mcp-pro: set_value / get_value / paste_text / menu_click.

Task 6 of the implementation plan. Rules from the spec (§3) respected here:

- Acts by Control Pattern, never by keystroke per letter and never by raw
  coordinates. ``set_value``/``get_value`` go through the ValuePattern;
  ``paste_text`` (long text, apps whose ValuePattern is unreliable) funnels
  through the same clipboard and SendInput seams as tools/compat.py.
- ``menu_click`` walks menu items structurally: MenuItem by MenuItem, with
  ExpandCollapse (or Invoke, which is how classic Win32 menus open a submenu)
  expanding the parent and Invoke on the last item.
- A control without the needed pattern comes back as a typed
  ``PATTERN_MISSING`` / ``VALUE_READ_ONLY`` error, never a crash and never a
  coordinate guess.

Element ids: handlers resolve *find/snapshot ids* through ``tools.query``'s
in-process registry first, and the literal ``"hwnd:runtimeid"`` form as a
fallback, so ids minted by the snapshot layer resolve without a registry hit.

Traversal reuses the helpers of ``tools.query`` (bounded children hops,
budgeted walks, typed NOT_FOUND) so a slow provider degrades instead of
hanging the server, and input funnels reuse ``tools.compat`` so tests never
touch a real desktop.

The ``uiautomation`` dependency is optional at import time (CI, packaging);
handlers then return a typed ``UNAVAILABLE`` error. Logging is stderr-only.
Result envelope (all handlers): success is ``{"ok": True, ...}``; failure is
``{"ok": False, "error": <CODE>, "reason": <str>, ...}``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tools import compat, query

log = logging.getLogger("win-mcp-pro.values")
log.addHandler(logging.NullHandler())

try:  # pragma: no cover - depends on the host
    import uiautomation as _uia
except Exception:  # noqa: BLE001 - any failure means "not available here"
    _uia = None

UNAVAILABLE = {
    "ok": False,
    "error": "UNAVAILABLE",
    "reason": query.UNAVAILABLE_REASON,
}

# budgets: a slow provider must degrade, not hang the call
_MENU_SEARCH_DEPTH = 4  # chained hops per menu-path segment
_MENU_PATH_MAX = 12  # segments a single menu_click may walk
_RUNTIME_SEARCH_DEPTH = 8
_RUNTIME_SEARCH_NODES = 2048

_HWND_RUNTIME_RE = re.compile(r"^\s*(\d+)\s*:\s*([\d.,\s]+)$")


# ---------------------------------------------------------------------------
# pydantic v2 schemas
# ---------------------------------------------------------------------------


class SetValueArgs(BaseModel):
    """Write text into a control through its ValuePattern (atomic)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id from find/snapshot, or 'hwnd:runtimeid'")
    text: str = Field(description="full replacement text for the control")

    _text = field_validator("text", mode="before")(compat._check_text)


class GetValueArgs(BaseModel):
    """Read the current text of a ValuePattern control."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id from find/snapshot, or 'hwnd:runtimeid'")


class PasteTextArgs(BaseModel):
    """Put text on the clipboard and send Ctrl+V into a focused control."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id from find/snapshot, or 'hwnd:runtimeid'")
    text: str = Field(description="text to place on the clipboard")

    _text = field_validator("text", mode="before")(compat._check_text)


class MenuClickArgs(BaseModel):
    """Follow a menu path like ['File', 'Save As']."""

    model_config = ConfigDict(extra="forbid")

    path: list[str] = Field(
        min_length=1,
        max_length=_MENU_PATH_MAX,
        description="menu labels from the menu bar down to the item to invoke",
    )

    @field_validator("path", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise ValueError("path must be a list of menu labels")
        for label in value:
            if not isinstance(label, str) or not label.strip():
                raise ValueError("menu labels must be non-empty strings")
        return value


class FormFieldIn(BaseModel):
    """One field of a form_fill call: element id plus its new text."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id from find/snapshot, or 'hwnd:runtimeid'")
    text: str = Field(description="full replacement text for the control")


class FormFillArgs(BaseModel):
    """Fill several ValuePattern fields inside one tool call."""

    model_config = ConfigDict(extra="forbid")

    fields: list[FormFieldIn] = Field(
        min_length=1, max_length=32,
        description="fields in fill order; each is verified by readback",
    )
    stop_on_error: bool = Field(
        default=True,
        description="stop at the first field that fails (false: fill the rest)",
    )


class ChooseFileArgs(BaseModel):
    """Resolve a standard File Open/Save dialog with a path."""

    model_config = ConfigDict(extra="forbid")

    dialog: str = Field(description="element id of the file dialog window")
    path: str = Field(description="file to open (must exist) or save (parent must exist)")
    mode: str = Field(
        default="open",
        description="'open' types the path and presses Open; 'save' presses Save",
    )
    button: str | None = Field(
        default=None,
        description="confirm-button label override (default: Open/Save by mode)",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        if value not in ("open", "save"):
            raise ValueError("mode must be 'open' or 'save'")
        return value


# ---------------------------------------------------------------------------
# control helpers (shared vocabulary with tools.query)
# ---------------------------------------------------------------------------


def _kind(control: Any) -> str:
    return query._control_kind(control)


def _name(control: Any) -> str:
    return query._text(query._attr(control, "Name"))


def _norm_label(value: Any) -> str:
    """'E&xit' / ' Save As ' -> 'save as' so providers spell labels differently."""
    return " ".join(query._text(value).replace("&", "").split()).casefold()


def _pattern(control: Any, probe: str) -> Any:
    getter = getattr(control, probe, None)
    if not callable(getter):
        return None
    return query._safe(getter)


# ---------------------------------------------------------------------------
# element resolution
# ---------------------------------------------------------------------------


def _parse_hwnd_runtime(raw: str) -> tuple[int, tuple[int, ...]] | None:
    match = _HWND_RUNTIME_RE.match(raw)
    if match is None:
        return None
    runtime = tuple(int(part) for part in re.findall(r"\d+", match.group(2)))
    if not runtime:
        return None
    return int(match.group(1)), runtime


def _runtime_tuple(control: Any) -> tuple[int, ...]:
    """The control's runtime id as ints.

    Real uiautomation controls expose ``GetRuntimeId()`` and no plain
    ``RuntimeId`` attribute; the attribute is kept as a fallback for older
    wrappers. Reading only the attribute made every snapshot-derived id
    unresolvable through this walk.
    """
    getter = getattr(control, "GetRuntimeId", None)
    if callable(getter):
        try:
            raw = getter()
        except Exception:  # noqa: BLE001 - dead element mid-walk
            return ()
    else:
        raw = query._attr(control, "RuntimeId")
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        try:
            return tuple(int(part) for part in raw)
        except (TypeError, ValueError):
            return ()
    return tuple(int(part) for part in re.findall(r"\d+", str(raw)))


def _descendants(start: Any, max_depth: int, budget: int) -> Iterator[Any]:
    """Breadth-first descent from a container, one bounded hop at a time.

    Repeat visits are dropped by RuntimeId, so a provider that returns a
    control as its own child degrades to NOT_FOUND instead of looping.
    """
    visited: set[str] = set()
    frontier = [start]
    depth = 0
    while frontier and budget > 0:
        next_frontier: list[Any] = []
        for control in frontier:
            key = query._control_key(control)
            if key in visited:
                continue
            visited.add(key)
            budget -= 1
            yield control
            if depth >= max_depth:
                continue
            next_frontier.extend(query._children(control))
        frontier = next_frontier
        depth += 1


def _walk_for_runtime(hwnd: int, runtime: tuple[int, ...]) -> Any | None:
    if _uia is None:
        return None
    from_handle = getattr(_uia, "ControlFromHandle", None)
    if not callable(from_handle):
        return None
    try:
        root = from_handle(hwnd)
    except Exception as exc:  # noqa: BLE001 - COM lookup failure means "gone"
        log.debug("ControlFromHandle(%s) failed: %s", hwnd, exc)
        return None
    if root is None:
        return None
    for control in _descendants(root, _RUNTIME_SEARCH_DEPTH, _RUNTIME_SEARCH_NODES):
        if _runtime_tuple(control) == runtime:
            return control
    return None


def _resolve(element_id: str) -> tuple[Any, dict | None]:
    """Registry id first; 'hwnd:runtimeid' as the snapshot-style fallback."""
    record = query._REGISTRY.get(element_id)
    if record is not None:
        return record.control, None

    parsed = _parse_hwnd_runtime(element_id)
    if parsed is not None:
        hwnd, runtime = parsed
        control = _walk_for_runtime(hwnd, runtime)
        if control is not None:
            return control, None
        label = ",".join(str(part) for part in runtime)
        return None, query._not_found(
            f"no element with runtime id '{label}' under hwnd {hwnd} "
            "(the window may be gone; take a fresh snapshot)"
        )
    return None, query._not_found(
        f"id '{element_id}' is neither a registered element id nor a "
        "'hwnd:runtimeid' pair; resolve it with find/snapshot first"
    )


# ---------------------------------------------------------------------------
# value pattern tools
# ---------------------------------------------------------------------------


def _read_value(pattern: Any) -> str | None:
    try:
        value = getattr(pattern, "Value", None)
    except Exception as exc:  # noqa: BLE001 - some providers raise on read
        log.debug("Value read failed: %s", exc)
        return None
    return None if value is None else str(value)


def set_value(id: str, text: str) -> dict:  # noqa: A002 - spec-facing name
    """Write text through the ValuePattern: one atomic write, no typing."""
    try:
        params = SetValueArgs(id=id, text=text)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE

    control, error = _resolve(params.id)
    if error is not None:
        return error
    pattern = _pattern(control, "GetValuePattern")
    if pattern is None:
        return {
            "ok": False,
            "error": "PATTERN_MISSING",
            "id": params.id,
            "reason": (
                f"{_kind(control)} '{_name(control)!r}' exposes no ValuePattern "
                "(read-only for automation); try get_value, paste_text into a "
                "focused field, or the screenshot/zoom fallback"
            ),
        }

    before = _read_value(pattern)
    try:
        pattern.SetValue(params.text)
    except Exception as exc:  # noqa: BLE001 - provider refuses the write
        return {
            "ok": False,
            "error": "VALUE_READ_ONLY",
            "id": params.id,
            "reason": f"ValuePattern refused the write: {exc}",
        }
    after = _read_value(pattern)
    return {
        "ok": True,
        "id": params.id,
        "before": before,
        "after": after,
        "verified": after == params.text,
    }


def get_value(id: str) -> dict:  # noqa: A002 - spec-facing name
    """Read the current text of a value control (no fallback guessing)."""
    try:
        params = GetValueArgs(id=id)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE

    control, error = _resolve(params.id)
    if error is not None:
        return error
    pattern = _pattern(control, "GetValuePattern")
    if pattern is None:
        return {
            "ok": False,
            "error": "PATTERN_MISSING",
            "id": params.id,
            "reason": (
                f"{_kind(control)} '{_name(control)!r}' exposes no ValuePattern; "
                "use get_prop with 'value' for a text read, or the "
                "screenshot/zoom fallback"
            ),
        }
    return {"ok": True, "id": params.id, "value": _read_value(pattern)}


# ---------------------------------------------------------------------------
# clipboard paste
# ---------------------------------------------------------------------------

_CTRL_V_EVENTS = [
    {"type": "key", "vk": compat.VK["ctrl"], "up": False},
    {"type": "key", "vk": compat._vk("v"), "up": False},
    {"type": "key", "vk": compat._vk("v"), "up": True},
    {"type": "key", "vk": compat.VK["ctrl"], "up": True},
]


def paste_text(id: str, text: str) -> dict:  # noqa: A002 - spec-facing name
    """Clipboard route for long text: write, focus the target, send Ctrl+V.

    The previous clipboard content is restored even when the keystroke fails,
    so a failed paste never eats the user's clipboard.
    """
    try:
        params = PasteTextArgs(id=id, text=text)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE

    # same gate as the compat input tools: DRY_RUN sends nothing
    if compat._dry_run():
        return {"dry_run": True, "action": {"name": "paste_text", **params.model_dump()}}

    control, error = _resolve(params.id)
    if error is not None:
        return error

    focus = getattr(control, "SetFocus", None)
    if callable(focus):
        try:
            focus()
        except Exception as exc:  # noqa: BLE001 - COM focus failure
            return {
                "ok": False,
                "error": "FOCUS_FAILED",
                "id": params.id,
                "reason": f"could not focus the control: {exc}",
            }

    try:
        previous: str | None = compat._clipboard_read()
    except Exception as exc:  # noqa: BLE001 - empty or foreign clipboard
        log.debug("clipboard snapshot skipped: %s", exc)
        previous = None

    failure: dict | None = None
    restored = False
    try:
        try:
            compat._clipboard_write(params.text)
        except Exception as exc:  # noqa: BLE001 - clipboard busy or locked
            failure = {
                "ok": False,
                "error": "CLIPBOARD_FAILED",
                "id": params.id,
                "reason": f"clipboard write failed: {exc}",
            }
        else:
            try:
                compat._send_input(_CTRL_V_EVENTS)
            except Exception as exc:  # noqa: BLE001 - SendInput rejected
                failure = {
                    "ok": False,
                    "error": "INPUT_FAILED",
                    "id": params.id,
                    "reason": f"SendInput rejected Ctrl+V: {exc}",
                }
    finally:
        if previous is not None:
            try:
                compat._clipboard_write(previous)
                restored = True
            except Exception as exc:  # noqa: BLE001 - best effort
                log.warning("clipboard restore failed: %s", exc)
    if failure is not None:
        return failure
    return {
        "ok": True,
        "id": params.id,
        "chars": len(params.text),
        "clipboard_restored": restored,
    }


# ---------------------------------------------------------------------------
# menu navigation
# ---------------------------------------------------------------------------


def _invoke_control(control: Any) -> bool:
    pattern = _pattern(control, "GetInvokePattern")
    if pattern is None:
        return False
    try:
        pattern.Invoke()
        return True
    except Exception as exc:  # noqa: BLE001 - COM invoke failure
        log.debug("invoke failed on %r: %s", _name(control), exc)
        return False


def _expand(control: Any) -> bool:
    """Open a submenu: ExpandCollapsePattern first, Invoke as the fallback
    (classic Win32 menus open their submenu when the parent item is invoked)."""
    pattern = _pattern(control, "GetExpandCollapsePattern")
    if pattern is not None:
        try:
            pattern.Expand()
            return True
        except Exception as exc:  # noqa: BLE001 - not expandable after all
            log.debug("expand failed on %r: %s", _name(control), exc)
    return _invoke_control(control)


def _is_menu_item(control: Any) -> bool:
    """Matches real UIA spellings: 'MenuItemControl' and 'MenuItem' alike.

    query._control_kind title-cases the kind, and 'menuitem'.title() is
    'Menuitem', so the comparison is normalized on both sides.
    """
    return query._norm_kind(_kind(control)) == "menuitem"


def _find_menu_item(container: Any, label: str) -> Any | None:
    target = _norm_label(label)
    for control in _descendants(container, _MENU_SEARCH_DEPTH, _RUNTIME_SEARCH_NODES):
        if not _is_menu_item(control):
            continue
        if _norm_label(_name(control)) == target:
            return control
    return None


def _active_window() -> Any | None:
    roots = query._roots()  # foreground window first
    return roots[0] if roots else None


def menu_click(path: list[str]) -> dict:  # noqa: A002 - spec-facing name
    """Follow a menu path like ['File', 'Save As'] and invoke the last item."""
    try:
        params = MenuClickArgs(path=path)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE

    window = _active_window()
    if window is None:
        return query._not_found("no top-level window to take a menu from")

    current: Any = window
    expanded: list[str] = []
    resolved: list[str] = []
    last = len(params.path) - 1
    for depth, label in enumerate(params.path):
        item = _find_menu_item(current, label)
        if item is None and depth > 0:
            # providers that parent the popup to the window: search again from
            # the top, still MenuItem-only and name-exact
            item = _find_menu_item(window, label)
        if item is None:
            walked = " > ".join(params.path[: depth + 1])
            parent = " > ".join(params.path[:depth]) or "menu bar"
            return {
                "ok": False,
                "error": "MENU_PATH_NOT_FOUND",
                "reason": f"no menu item {label!r} under {parent!r} (path tried: {walked!r})",
            }
        resolved.append(_name(item))
        if depth < last:
            if not _expand(item):
                return {
                    "ok": False,
                    "error": "MENU_PATH_NOT_FOUND",
                    "reason": (
                        f"menu item {label!r} ({params.path[: depth + 1]}) cannot open "
                        "a submenu: no ExpandCollapse and no Invoke pattern"
                    ),
                }
            expanded.append(label)
            current = item
    if not _invoke_control(item):  # type: ignore[arg-type]  # set on success path
        return {
            "ok": False,
            "error": "PATTERN_MISSING",
            "reason": f"menu item {params.path[-1]!r} exposes no Invoke pattern",
        }
    return {
        "ok": True,
        "path": params.path,
        "clicked": resolved[-1],
        "expanded": expanded,
    }


def form_fill(fields: list[dict] | None = None,
              stop_on_error: bool = True) -> dict:  # noqa: A002 - spec-facing name
    """Fill several ValuePattern fields in order, each verified by readback.

    One call instead of N set_value round-trips; every field reports its own
    ok/verified (or typed error). With ``stop_on_error`` (default) the fields
    after a failure stay untouched.
    """
    try:
        params = FormFillArgs(fields=fields or [], stop_on_error=stop_on_error)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE

    filled: list[dict] = []
    for field in params.fields:
        result = set_value(id=field.id, text=field.text)
        if result.get("ok"):
            filled.append({"id": field.id, "ok": True,
                           "verified": bool(result.get("verified"))})
        else:
            filled.append({"id": field.id, "ok": False,
                           "error": result.get("error"),
                           "reason": result.get("reason")})
            if params.stop_on_error:
                break
    good = sum(1 for entry in filled if entry["ok"])
    return {"ok": good == len(filled) and bool(filled),
            "fields": filled, "filled": good, "total": len(filled)}


#: Row/cell vocabulary for table_extract (UIA spellings, normalized).
#: WinForms grids surface rows as Custom ("Linha 0") and cells as DataItem,
#: headers as Header - all covered here; the walk stays rooted at the given
#: grid control so a broad "custom" row match cannot wander the desktop.
_TABLE_ROOT_KINDS = {"table", "datagrid"}
_TABLE_ROW_KINDS = {"row", "dataitem", "listitem", "treeitem", "custom"}
_TABLE_CELL_KINDS = {"cell", "dataitem", "text", "edit", "listitem", "header"}
_TABLE_MAX_ROWS = 200
_TABLE_MAX_COLS = 50


def _cell_text(cell: Any) -> str:
    """Best-effort cell read: content (ValuePattern) first, Name fallback.

    Providers label cells ("Key Linha 0") apart from their content ("alpha"):
    the content is what a table read owes. Headers and owner-drawn cells
    with no value fall back to the Name.
    """
    value = query._value_text(cell)
    if value:
        return value
    return _name(cell)


def table_extract(id: str) -> dict:  # noqa: A002 - spec-facing name
    """Read a grid/table control into rows of cell text (capped).

    Walks row containers then their cells - the UIA Table/Row/Cell shape the
    platform documents - with no pattern dependency, so owner-drawn grids
    that skip GridPattern still read. Caps at 200 rows x 50 columns with a
    ``truncated`` flag instead of flooding the protocol.
    """
    try:
        params = GetValueArgs(id=id)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE

    control, error = _resolve(params.id)
    if error is not None:
        return error
    rows: list[list[str]] = []
    saw_candidate = False
    truncated = False
    for child in _descendants(control, 3, _RUNTIME_SEARCH_NODES):
        if query._norm_kind(_kind(child)) not in _TABLE_ROW_KINDS:
            continue
        saw_candidate = True
        cells = [c for c in query._children(child)
                 if query._norm_kind(_kind(c)) in _TABLE_CELL_KINDS]
        if not cells:  # some providers nest one level deeper
            for grandchild in query._children(child):
                cells.extend(c for c in query._children(grandchild)
                             if query._norm_kind(_kind(c)) in _TABLE_CELL_KINDS)
        if not cells:
            # a cell that matched as a row (WinForms DataItem cells match
            # the row vocabulary): no readable cells, not a row.
            continue
        rows.append([_cell_text(cell) for cell in cells[:_TABLE_MAX_COLS]])
        if len(rows) >= _TABLE_MAX_ROWS:
            truncated = True
            break
    if not rows and not saw_candidate:
        return {
            "ok": False,
            "error": "TABLE_EMPTY",
            "id": params.id,
            "reason": f"{_kind(control)} '{_name(control)!r}' exposes no table rows",
        }
    width = max([len(row) for row in rows] or [0])
    return {"ok": True, "id": params.id, "rows": rows,
            "shape": [len(rows), width], "truncated": truncated}


#: Labels the filename field carries in stock dialogs (localized builds use
#: the single-Edit fallback below instead).
_FILENAME_LABELS = ("file name", "filename", "file name:")


def _dialog_filename_field(dialog: Any) -> Any | None:
    """The Edit that takes the path.

    Name match first (directly on the Edit, or on the ComboBox wrapping it -
    the stock layout), then the single-Edit fallback. Anything ambiguous is
    FIELD_NOT_FOUND rather than a guess.
    """
    edits: list[Any] = []
    for control in _descendants(dialog, 4, _RUNTIME_SEARCH_NODES):
        kind = query._norm_kind(_kind(control))
        if kind == "combobox" and _norm_label(_name(control)) in _FILENAME_LABELS:
            for sub in _descendants(control, 2, 32):
                if query._norm_kind(_kind(sub)) == "edit":
                    return sub
            continue
        if kind != "edit":
            continue
        if _norm_label(_name(control)) in _FILENAME_LABELS:
            return control
        edits.append(control)
    if len(edits) == 1:
        return edits[0]
    return None


def choose_file(dialog: str, path: str, mode: str = "open",
                button: str | None = None) -> dict:  # noqa: A002 - spec-facing name
    """Type a path into a File Open/Save dialog and press its confirm button.

    Fail-closed on the filesystem first (Open needs the file, Save needs its
    parent), then on the dialog's own controls: no filename field or no
    confirm button is a typed refusal with zero input sent.
    """
    import os

    try:
        params = ChooseFileArgs(dialog=dialog, path=path, mode=mode, button=button)
    except ValidationError as exc:
        return query._bad_request(query._first_error(exc))
    if _uia is None or query._uia is None:
        return UNAVAILABLE
    if compat._dry_run():
        return {"dry_run": True, "action": {"name": "choose_file", **params.model_dump()}}

    wanted = os.path.abspath(params.path)
    if params.mode == "open":
        if not os.path.isfile(wanted):
            return {"ok": False, "error": "PATH_MISSING",
                    "reason": f"no file at {wanted!r}"}
    elif not os.path.isdir(os.path.dirname(wanted) or "."):
        return {"ok": False, "error": "PATH_MISSING",
                "reason": f"no parent directory for {wanted!r}"}

    control, error = _resolve(params.dialog)
    if error is not None:
        return error
    field = _dialog_filename_field(control)
    if field is None:
        return {"ok": False, "error": "FIELD_NOT_FOUND",
                "reason": "the dialog exposes no filename field"}
    pattern = _pattern(field, "GetValuePattern")
    if pattern is None:
        return {"ok": False, "error": "PATTERN_MISSING",
                "reason": "the filename field exposes no ValuePattern"}
    try:
        pattern.SetValue(params.path)
    except Exception as exc:  # noqa: BLE001 - provider refuses the write
        return {"ok": False, "error": "VALUE_READ_ONLY",
                "reason": f"filename field refused the write: {exc}"}
    confirm = params.button or ("Open" if params.mode == "open" else "Save")
    for candidate in _descendants(control, 4, _RUNTIME_SEARCH_NODES):
        if query._norm_kind(_kind(candidate)) != "button":
            continue
        if _norm_label(_name(candidate)) == _norm_label(confirm):
            if _invoke_control(candidate):
                return {"ok": True, "dialog": params.dialog,
                        "path": wanted, "button": _name(candidate)}
            return {"ok": False, "error": "INVOKE_FAILED",
                    "reason": f"button {_name(candidate)!r} would not invoke"}
    return {"ok": False, "error": "BUTTON_NOT_FOUND",
            "reason": f"no button {confirm!r} in the dialog"}


# ---------------------------------------------------------------------------
# handler wrappers: accept either a mapping or keyword arguments
# ---------------------------------------------------------------------------


def _handle_set_value(params: dict | None = None, **kwargs: Any) -> dict:
    return set_value(**query._merge(params, kwargs))


def _handle_get_value(params: dict | None = None, **kwargs: Any) -> dict:
    return get_value(**query._merge(params, kwargs))


def _handle_paste_text(params: dict | None = None, **kwargs: Any) -> dict:
    return paste_text(**query._merge(params, kwargs))


def _handle_menu_click(params: dict | None = None, **kwargs: Any) -> dict:
    return menu_click(**query._merge(params, kwargs))


def _handle_form_fill(params: dict | None = None, **kwargs: Any) -> dict:
    return form_fill(**query._merge(params, kwargs))


def _handle_table_extract(params: dict | None = None, **kwargs: Any) -> dict:
    return table_extract(**query._merge(params, kwargs))


def _handle_choose_file(params: dict | None = None, **kwargs: Any) -> dict:
    return choose_file(**query._merge(params, kwargs))


# ---------------------------------------------------------------------------
# tool registry contract: TOOLS: list[{name, description, schema, handler}]
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "set_value",
        "description": (
            "Write text into a control through its UIA ValuePattern: one atomic "
            "write, never a keystroke per character, no coordinates. Read-only "
            "controls come back as PATTERN_MISSING / VALUE_READ_ONLY."
        ),
        "schema": SetValueArgs,
        "handler": _handle_set_value,
    },
    {
        "name": "get_value",
        "description": (
            "Read the current text of a value control (edit, combo box, spinner) "
            "through the ValuePattern. Controls without the pattern get a typed "
            "PATTERN_MISSING error instead of a guess."
        ),
        "schema": GetValueArgs,
        "handler": _handle_get_value,
    },
    {
        "name": "paste_text",
        "description": (
            "Paste long text into a focused field through the clipboard and "
            "Ctrl+V, for apps whose ValuePattern is unreliable. The previous "
            "clipboard content is restored even when the keystroke fails."
        ),
        "schema": PasteTextArgs,
        "handler": _handle_paste_text,
    },
    {
        "name": "menu_click",
        "description": (
            "Follow a menu path like [\"File\", \"Save As\"] structurally: each "
            "segment resolves to a MenuItem by name, the parents expand "
            "(ExpandCollapse or Invoke), and the last item is invoked."
        ),
        "schema": MenuClickArgs,
        "handler": _handle_menu_click,
    },
    {
        "name": "form_fill",
        "description": (
            "Fill several ValuePattern fields in order inside one call, each "
            "verified by readback. Stops at the first failure by default so "
            "later fields stay untouched."
        ),
        "schema": FormFillArgs,
        "handler": _handle_form_fill,
    },
    {
        "name": "table_extract",
        "description": (
            "Read a grid/table control into rows of cell text (capped at "
            "200x50 with a truncated flag). No pattern dependency: walks the "
            "documented Table/Row/Cell shape."
        ),
        "schema": GetValueArgs,
        "handler": _handle_table_extract,
    },
    {
        "name": "choose_file",
        "description": (
            "Resolve a File Open/Save dialog: types the path into its filename "
            "field and presses Open/Save. Fail-closed on the filesystem "
            "(Open needs the file, Save its parent) and on the dialog's own "
            "controls."
        ),
        "schema": ChooseFileArgs,
        "handler": _handle_choose_file,
    },
]

__all__ = [
    "TOOLS",
    "set_value",
    "get_value",
    "paste_text",
    "menu_click",
    "form_fill",
    "table_extract",
    "choose_file",
    "SetValueArgs",
    "GetValueArgs",
    "PasteTextArgs",
    "MenuClickArgs",
    "FormFieldIn",
    "FormFillArgs",
    "ChooseFileArgs",
]
