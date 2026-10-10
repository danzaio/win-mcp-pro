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
]

__all__ = [
    "TOOLS",
    "set_value",
    "get_value",
    "paste_text",
    "menu_click",
    "SetValueArgs",
    "GetValueArgs",
    "PasteTextArgs",
    "MenuClickArgs",
]
