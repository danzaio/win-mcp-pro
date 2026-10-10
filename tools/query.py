"""Tree query tools for win-mcp-pro: find / dump_tree / get_prop / get_patterns.

Task 4 of the implementation plan. Rules from the spec (§3) respected here:

- Tree-first: elements are resolved by Name / AutomationId / ClassName /
  ControlType. No coordinates, no screenshots.
- Search is *chained* one level at a time. The tree is walked breadth-first,
  and every hop asks a single control for its children, which is the
  equivalent of ``searchDepth=1`` per hop (window -> parent -> child). A
  single deep search issued from the UIA root is never used, because that is
  the traversal that gets stuck on pathological providers. A node four levels
  down is still reachable; it just costs four chained hops.
- ``find`` registers what it resolves in an in-process registry and hands out
  a stable id plus a human readable path. The act layer reuses that id instead
  of re-walking the tree.
- A miss is a typed ``NOT_FOUND`` error carrying a reason and the vision
  fallback suggestion, never a coordinate guess.

The ``uiautomation`` dependency is optional at import time: this module must
import on machines without it (CI, packaging). Handlers then return a typed
``UNAVAILABLE`` error instead of raising.

Result envelope (all handlers): success is ``{"ok": True, ...}``; failure is
``{"ok": False, "error": <CODE>, "reason": <str>, ...}``.

Logging is stderr-only (MCP stdio never writes to stdout); nothing here
evaluates or logs any credential.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("win-mcp-pro.query")
log.addHandler(logging.NullHandler())

# ---------------------------------------------------------------------------
# optional dependency
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on the host
    import uiautomation as _uia
except Exception:  # noqa: BLE001 - any failure means "not available here"
    _uia = None

UNAVAILABLE_REASON = (
    "uiautomation is not available in this Python environment "
    "(install it with: pip install uiautomation)"
)

# traversal budgets: a slow provider must degrade, not hang the server
_MAX_DEPTH = 8
_MAX_TREE_DEPTH = 4
_MAX_CHILDREN = 512
_MAX_NODES = 4096
_REGISTRY_LIMIT = 512


# ---------------------------------------------------------------------------
# pydantic v2 schemas
# ---------------------------------------------------------------------------


class FindParams(BaseModel):
    """Resolve an element by tree properties (never by coordinates)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="exact Name, case-insensitive")
    automation_id: str | None = Field(default=None, description="AutomationId, exact")
    class_name: str | None = Field(default=None, description="ClassName, exact")
    kind: str | None = Field(
        default=None, description="ControlType, e.g. 'Edit' or 'EditControl'"
    )
    max_depth: int = Field(
        default=_MAX_DEPTH, ge=1, le=_MAX_DEPTH, description="chained hops below the window"
    )

class DumpTreeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id returned by find/dump_tree/snapshot")
    depth: int = Field(
        default=2, ge=0, le=32, description="child levels to expand"
    )


class GetPropParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id")
    prop: str = Field(description="property name; see tool description")


class GetPatternsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="element id")


# ---------------------------------------------------------------------------
# control readers: work with uiautomation and with the test doubles alike
# ---------------------------------------------------------------------------


def _attr(control: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            value = getattr(control, name, None)
        except Exception:  # noqa: BLE001 - a dead COM property must not abort
            value = None
        if value is not None:
            return value
    return default


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _norm_kind(value: Any) -> str:
    """'EditControl' / 'edit' -> 'edit' so friendly spellings all match."""
    kind = _text(value).strip().lower()
    if kind.endswith("control"):
        kind = kind[: -len("control")]
    return kind.strip()


def _control_kind(control: Any) -> str:
    """Canonical control type: 'EditControl' / 'edit' -> 'Edit'."""
    raw = _attr(control, "ControlTypeName", "LocalizedControlType")
    return _norm_kind(raw).title() or "Unknown"


def _control_key(control: Any) -> str:
    """Stable-ish identity within one walk (uia RuntimeId when present)."""
    runtime_id = _attr(control, "RuntimeId")
    if isinstance(runtime_id, (list, tuple)):
        return "|".join(str(part) for part in runtime_id)
    if runtime_id:
        return str(runtime_id)
    handle = _attr(control, "NativeWindowHandle")
    if handle:
        return f"hwnd:{handle}|{_text(_attr(control, 'Name'))}|{_control_kind(control)}"
    return f"obj:{id(control)}"


# ---------------------------------------------------------------------------
# registry: id -> element record (so the act layer never re-walks the tree)
# ---------------------------------------------------------------------------


class _Record:
    __slots__ = ("id", "path", "parent_path", "name", "kind", "automation_id",
                 "class_name", "window", "window_handle", "control")

    def __init__(self, element_id: str, path: str, parent_path: str, name: str,
                 kind: str, automation_id: str, class_name: str, window: str,
                 window_handle: Any, control: Any) -> None:
        self.id = element_id
        self.path = path
        self.parent_path = parent_path
        self.name = name
        self.kind = kind
        self.automation_id = automation_id
        self.class_name = class_name
        self.window = window
        self.window_handle = window_handle
        self.control = control

    def element(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "window": self.window,
        }


_REGISTRY: "OrderedDict[str, _Record]" = OrderedDict()


def _registry_clear() -> None:
    """Test seam: drop every cached element record."""
    _REGISTRY.clear()


def _remember(record: _Record) -> str:
    _REGISTRY[record.id] = record
    while len(_REGISTRY) > _REGISTRY_LIMIT:
        _REGISTRY.popitem(last=False)
    return record.id


def _element_id(path: str, window: str) -> str:
    digest = hashlib.sha1(f"{window}|{path}".encode("utf-8", "replace")).hexdigest()
    return digest[:12]


def _path_segment(control: Any) -> str:
    """Human readable hop: 'Edit:Text Editor', bare 'Pane' when unnamed.

    A name that merely repeats the control type (the classic 'Pane' pane,
    'Window' host) adds nothing to the path, so it is dropped.
    """
    kind = _control_kind(control)
    name = _text(_attr(control, "Name")).strip()
    if name and name.lower() != kind.lower():
        return f"{kind}:{name}"
    return kind


def _register(control: Any, parent_path: str, window: str,
              window_handle: Any = None) -> _Record:
    path = f"{parent_path} > {_path_segment(control)}" if parent_path else _path_segment(control)
    record = _Record(
        element_id=_element_id(path, window),
        path=path,
        parent_path=parent_path,
        name=_text(_attr(control, "Name")),
        kind=_control_kind(control),
        automation_id=_text(_attr(control, "AutomationId")),
        class_name=_text(_attr(control, "ClassName")),
        window=window,
        window_handle=window_handle,
        control=control,
    )
    _remember(record)
    return record


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------


def _children(control: Any) -> list:
    """One chained hop: the children of a single control (depth-1 step)."""
    try:
        kids = control.GetChildren() or []
    except Exception as exc:  # noqa: BLE001 - a stuck child must not kill the walk
        log.warning("children query failed on %r: %s", control, exc)
        return []
    return list(kids)[:_MAX_CHILDREN]


def _roots() -> list:
    """Top-level windows: the foreground window first, then everything else."""
    if _uia is None:
        return []
    root = _uia.GetRootControl()
    foreground = None
    get_foreground = getattr(_uia, "GetForegroundWindow", None)
    if callable(get_foreground):
        try:
            foreground = get_foreground()
        except Exception:  # noqa: BLE001 - optional fast path
            foreground = None
    windows = [] if root is None else _children(root)
    if foreground is not None and foreground in windows:
        windows.remove(foreground)
        windows.insert(0, foreground)
    return windows


def _matches(control: Any, name: str | None, automation_id: str | None,
             class_name: str | None, kind: str | None) -> bool:
    if name is not None and _text(_attr(control, "Name")).strip().lower() != name.strip().lower():
        return False
    if automation_id is not None and _text(_attr(control, "AutomationId")) != automation_id:
        return False
    if class_name is not None and _text(_attr(control, "ClassName")) != class_name:
        return False
    if kind is not None and _norm_kind(_control_kind(control)) != _norm_kind(kind):
        return False
    return True


def _walk(predicate: Callable[[Any], bool], max_depth: int) -> _Record | None:
    """Breadth-first walk; every hop is a single-level children query.

    Returns the shallowest match (BFS order), which is the behaviour a chained
    ``searchDepth=1`` search has and a root-issued deep search does not.
    """
    budget = _MAX_NODES
    frontier: list[tuple] = [
        (control, "", _text(_attr(control, "Name")),
         _attr(control, "NativeWindowHandle"), 0)
        for control in _roots()
    ]
    visited: set[str] = set()
    while frontier and budget > 0:
        next_frontier: list[tuple] = []
        for control, parent_path, window, window_handle, depth in frontier:
            key = _control_key(control)
            if key in visited:
                continue
            visited.add(key)
            budget -= 1
            if budget <= 0:
                break
            if predicate(control):
                return _register(control, parent_path, window, window_handle)
            if depth >= max_depth:
                continue
            segment = _path_segment(control)
            child_path = f"{parent_path} > {segment}" if parent_path else segment
            for child in _children(control):
                next_frontier.append((child, child_path, window, window_handle, depth + 1))
                budget -= 1
        frontier = next_frontier
    return None


# ---------------------------------------------------------------------------
# property and pattern probes
# ---------------------------------------------------------------------------


def _rect(control: Any) -> dict | None:
    rect = _attr(control, "BoundingRectangle")
    if rect is None:
        return None
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        left, top, right, bottom = rect
    else:
        left = _attr(rect, "left", "Left")
        top = _attr(rect, "top", "Top")
        right = _attr(rect, "right", "Right")
        bottom = _attr(rect, "bottom", "Bottom")
    if None in (left, top, right, bottom):
        return None
    return {"left": int(left), "top": int(top), "right": int(right), "bottom": int(bottom)}


def _value_text(control: Any) -> str | None:
    for probe in ("GetValuePattern", "GetTextPattern"):
        getter = getattr(control, probe, None)
        if not callable(getter):
            continue
        try:
            pattern = getter()
            value = getattr(pattern, "Value", None) if pattern is not None else None
        except Exception:  # noqa: BLE001 - probe failure means "not readable"
            continue
        if value is not None:
            return str(value)
    return None


def _safe(getter: Callable[[], Any]) -> Any:
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


_PROP_READERS: dict[str, Callable[[Any], Any]] = {
    "name": lambda c: _text(_attr(c, "Name")),
    "automation_id": lambda c: _text(_attr(c, "AutomationId")),
    "class_name": lambda c: _text(_attr(c, "ClassName")),
    "kind": _control_kind,
    "localized_kind": lambda c: _text(_attr(c, "LocalizedControlType")),
    "pid": lambda c: _attr(c, "ProcessId"),
    "rect": _rect,
    "is_enabled": lambda c: bool(_attr(c, "IsEnabled", default=True)),
    "has_keyboard_focus": lambda c: bool(_attr(c, "HasKeyboardFocus", default=False)),
    "is_offscreen": lambda c: bool(_attr(c, "IsOffscreen", default=False)),
    "is_password": lambda c: bool(_attr(c, "IsPassword", default=False)),
    "accelerator_key": lambda c: _text(_attr(c, "AcceleratorKey")),
    "access_key": lambda c: _text(_attr(c, "AccessKey")),
    "help_text": lambda c: _text(_attr(c, "HelpText")),
    "item_status": lambda c: _text(_attr(c, "ItemStatus")),
    "item_type": lambda c: _text(_attr(c, "ItemType")),
    "value": _value_text,
}

# props that live on the registry record rather than the control itself
_RECORD_READERS: dict[str, Callable[[_Record], Any]] = {
    "id": lambda r: r.id,
    "path": lambda r: r.path,
    "window": lambda r: r.window,
    "handle": lambda r: r.window_handle,
}

_PATTERN_PROBES: tuple[tuple[str, str], ...] = (
    ("dock", "GetDockPattern"),
    ("expand_collapse", "GetExpandCollapsePattern"),
    ("grid", "GetGridPattern"),
    ("grid_item", "GetGridItemPattern"),
    ("invoke", "GetInvokePattern"),
    ("item_container", "GetItemContainerPattern"),
    ("multiple_view", "GetMultipleViewPattern"),
    ("range_value", "GetRangeValuePattern"),
    ("scroll", "GetScrollPattern"),
    ("scroll_item", "GetScrollItemPattern"),
    ("selection", "GetSelectionPattern"),
    ("selection_item", "GetSelectionItemPattern"),
    ("table", "GetTablePattern"),
    ("table_item", "GetTableItemPattern"),
    ("text", "GetTextPattern"),
    ("toggle", "GetTogglePattern"),
    ("transform", "GetTransformPattern"),
    ("value", "GetValuePattern"),
    ("virtualized_item", "GetVirtualizedItemPattern"),
    ("window", "GetWindowPattern"),
)


# ---------------------------------------------------------------------------
# error helpers
# ---------------------------------------------------------------------------


def _not_found(reason: str) -> dict:
    return {
        "ok": False,
        "error": "NOT_FOUND",
        "reason": reason,
        "suggestion": (
            "the UIA tree cannot resolve this element (custom render, no provider, "
            "or a stale id). Take a fresh snapshot, then use the logged "
            "screenshot/zoom vision fallback as a last resort."
        ),
    }


def _bad_request(reason: str) -> dict:
    return {"ok": False, "error": "BAD_REQUEST", "reason": reason}


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "input"
    return f"invalid arguments: {location} {first.get('msg', 'is invalid')}"


def _lookup(element_id: str) -> _Record | dict:
    if _uia is None:
        return {"ok": False, "error": "UNAVAILABLE", "reason": UNAVAILABLE_REASON}
    record = _REGISTRY.get(element_id)
    if record is None:
        return _not_found(f"no element registered under id '{element_id}'")
    return record


def _merge(params: dict | None, kwargs: dict) -> dict:
    data = dict(params or {})
    data.update(kwargs)
    return data


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------


def find(name: str | None = None, automation_id: str | None = None,
         class_name: str | None = None, kind: str | None = None,
         max_depth: int = _MAX_DEPTH) -> dict:
    """Resolve one element to a stable id plus its window -> parent -> child path.

    Accepts keyword arguments or a single params mapping (the shape the MCP
    layer forwards from a JSON-RPC call): ``find({"name": "Save"})``.
    """
    if isinstance(name, Mapping):
        mapping = {str(key): value for key, value in name.items()}
        try:
            return find(**mapping)
        except TypeError as exc:
            return _bad_request(f"invalid arguments: {exc}")
    try:
        params = FindParams(name=name, automation_id=automation_id, class_name=class_name,
                            kind=kind, max_depth=max_depth)
    except ValidationError as exc:
        return _bad_request(_first_error(exc))

    if _uia is None:
        return {"ok": False, "error": "UNAVAILABLE", "reason": UNAVAILABLE_REASON}
    if all(value is None for value in (params.name, params.automation_id,
                                       params.class_name, params.kind)):
        return _bad_request(
            "find needs at least one of name, automation_id, class_name, kind"
        )

    record = _walk(
        lambda control: _matches(control, params.name, params.automation_id,
                                 params.class_name, params.kind),
        max_depth=params.max_depth,
    )
    if record is None:
        criteria = [f"{key}={value}" for key, value in (
            ("name", params.name), ("automation_id", params.automation_id),
            ("class_name", params.class_name), ("kind", params.kind),
        ) if value is not None]
        return _not_found(
            f"no element matching {', '.join(criteria)} within {params.max_depth} "
            "chained hops of any top-level window"
        )
    return {"ok": True, "element": record.element()}


def dump_tree(id: str, depth: int = 2) -> dict:  # noqa: A002 - spec-facing name
    """Expand a registered element into nested children, registering every node."""
    try:
        params = DumpTreeParams(id=id, depth=depth)
    except ValidationError as exc:
        return _bad_request(_first_error(exc))
    record = _lookup(params.id)
    if isinstance(record, dict):
        return record

    state = {"nodes": 0, "truncated": False}

    def expand(control: Any, parent_path: str, window: str, window_handle: Any,
               level: int) -> dict:
        node = _register(control, parent_path, window, window_handle)
        children_out: list[dict] = []
        if level < params.depth:
            for child in _children(control):
                state["nodes"] += 1
                if state["nodes"] > _MAX_NODES:
                    state["truncated"] = True
                    break
                children_out.append(
                    expand(child, node.path, window, window_handle, level + 1)
                )
        return {
            "id": node.id,
            "path": node.path,
            "name": node.name,
            "kind": node.kind,
            "children": children_out,
        }

    tree = expand(record.control, record.parent_path, record.window,
                  record.window_handle, 0)
    return {
        "ok": True,
        "element": record.element(),
        "depth": params.depth,
        "children": tree["children"],
        "truncated": state["truncated"],
    }


def get_prop(id: str, prop: str) -> dict:  # noqa: A002 - spec-facing name
    """Read one tree property off a registered element (JSON-serializable)."""
    try:
        params = GetPropParams(id=id, prop=prop)
    except ValidationError as exc:
        return _bad_request(_first_error(exc))
    record = _lookup(params.id)
    if isinstance(record, dict):
        return record

    reader = _PROP_READERS.get(params.prop.strip().lower())
    record_reader = _RECORD_READERS.get(params.prop.strip().lower())
    if reader is None and record_reader is None:
        return {
            "ok": False,
            "error": "UNKNOWN_PROP",
            "reason": f"'{params.prop}' is not a known property",
            "available": sorted(_PROP_READERS) + sorted(_RECORD_READERS),
        }
    try:
        value = reader(record.control) if reader else record_reader(record)
    except Exception as exc:  # noqa: BLE001 - COM read failure, not a crash
        return {
            "ok": False,
            "error": "PROP_UNAVAILABLE",
            "reason": f"could not read '{params.prop}' on {record.path}: {exc}",
        }
    return {"ok": True, "id": record.id, "prop": params.prop.lower(), "value": value}


def get_patterns(id: str) -> dict:  # noqa: A002 - spec-facing name
    """List the Control Patterns an element supports (the act layer's menu)."""
    try:
        params = GetPatternsParams(id=id)
    except ValidationError as exc:
        return _bad_request(_first_error(exc))
    record = _lookup(params.id)
    if isinstance(record, dict):
        return record

    supported: list[str] = []
    for pattern_name, probe_name in _PATTERN_PROBES:
        probe = getattr(record.control, probe_name, None)
        if not callable(probe):
            continue
        if _safe(probe) is not None:
            supported.append(pattern_name)
    return {"ok": True, "id": record.id, "patterns": supported}


# ---------------------------------------------------------------------------
# handler wrappers: accept either a mapping or keyword arguments
# ---------------------------------------------------------------------------


def _handle_find(params: dict | None = None, **kwargs: Any) -> dict:
    return find(**_merge(params, kwargs))


def _handle_dump_tree(params: dict | None = None, **kwargs: Any) -> dict:
    return dump_tree(**_merge(params, kwargs))


def _handle_get_prop(params: dict | None = None, **kwargs: Any) -> dict:
    return get_prop(**_merge(params, kwargs))


def _handle_get_patterns(params: dict | None = None, **kwargs: Any) -> dict:
    return get_patterns(**_merge(params, kwargs))


# ---------------------------------------------------------------------------
# tool registry contract: TOOLS: list[{name, description, schema, handler}]
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "find",
        "description": (
            "Resolve a UI element by name / automation id / class name / control "
            "type and return a stable id plus its window -> parent -> child path. "
            "Search is chained one level at a time (searchDepth=1 per hop), never "
            "a single deep search from the root, and the shallowest match wins. "
            "No coordinates, no image."
        ),
        "schema": FindParams,
        "handler": _handle_find,
    },
    {
        "name": "dump_tree",
        "description": (
            "Expand a registered element into nested children (depth 0-4, default "
            "2). Every returned node carries its own id, so a later action can "
            "address it without another search."
        ),
        "schema": DumpTreeParams,
        "handler": _handle_dump_tree,
    },
    {
        "name": "get_prop",
        "description": (
            "Read one property of a registered element as JSON: name, kind, "
            "automation_id, class_name, rect, handle, pid, is_enabled, "
            "has_keyboard_focus, is_offscreen, is_password, accelerator_key, "
            "access_key, help_text, item_status, item_type, value, localized_kind."
        ),
        "schema": GetPropParams,
        "handler": _handle_get_prop,
    },
    {
        "name": "get_patterns",
        "description": (
            "List the Control Patterns an element supports (invoke, value, "
            "selection, expand_collapse, text, ...). Use it before calling an "
            "action tool so an unsupported pattern comes back as a typed error."
        ),
        "schema": GetPatternsParams,
        "handler": _handle_get_patterns,
    },
]

__all__ = ["TOOLS", "find", "dump_tree", "get_prop", "get_patterns",
           "FindParams", "DumpTreeParams", "GetPropParams", "GetPatternsParams"]
