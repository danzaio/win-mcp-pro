"""Task 5: Control Pattern actions.

Every act tool here goes through a UIA *control pattern* (InvokePattern,
TogglePattern, SelectionItemPattern, ExpandCollapsePattern, ScrollItemPattern)
or through ``IUIAutomationElement::SetFocus`` for ``focus``. There is no
coordinate click and no synthesised input in this module, by design: if the
tree says an element cannot do something, the answer is a typed error and not
a mouse nudge.

Element ids are ``"hwnd:runtimeid"`` strings as produced by the snapshot/find
layers: the window handle, a colon, then the UIA runtime id (an int path),
serialised with any of ``,.,;:|`` separators, e.g. ``"17524:42,8,2"``.
Task 4 publishes an element for a given id with :func:`register_element`;
anything not found there is looked up by walking the window's UIA subtree and
matching runtime ids, under a node and time budget so a pathological tree
skips instead of hanging.

Logging is stderr-only (``logging``); this module never prints to stdout, which
is the MCP stdio transport's channel.
"""

from __future__ import annotations

import itertools
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from itertools import islice
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

LOG = logging.getLogger("win-mcp-pro.tools.patterns")

try:  # import-safe: the server must boot (and tests must run) without UIA
    import uiautomation as _uia  # type: ignore
except Exception:  # pragma: no cover - depends on the machine
    _uia = None

# --- typed errors ------------------------------------------------------------

ERR_INVALID_ID = "INVALID_ID"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_PATTERN_UNSUPPORTED = "PATTERN_UNSUPPORTED"
ERR_PATTERN_FAILED = "PATTERN_FAILED"
ERR_ELEMENT_DISABLED = "ELEMENT_DISABLED"
ERR_ELEVATION_REQUIRED = "ELEVATION_REQUIRED"
ERR_ACTION_NO_EFFECT = "ACTION_NO_EFFECT"
ERR_UNKNOWN_ACTION = "UNKNOWN_ACTION"

# COM/UIA HRESULTS worth naming.
_HR_ACCESS_DENIED = 0x80070005
_HR_ELEMENT_NOT_AVAILABLE = 0x80040201


class PatternError(Exception):
    """A foreseeable failure, returned to the caller instead of bubbling up."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


# --- pydantic v2 models -----------------------------------------------------


class ActionArgs(BaseModel):
    """Arguments of every pattern action tool: an element id."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        description='Element id "hwnd:runtimeid" (e.g. "17524:42,8,2") from snapshot/find',
        min_length=1,
    )


class ActionResult(BaseModel):
    """Outcome of one pattern action: state before, state after, error if any."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    action: str
    element_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    error: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class PatternTool:
    """A tool definition, in the attribute style the registry normalises."""

    name: str
    description: str
    schema: Any  # pydantic BaseModel subclass; the registry exports its JSON schema
    handler: Any


# --- element registry (ids published by the snapshot/find layers) ------------

_ELEMENTS: dict[str, Any] = {}
_MAX_REGISTRY = 1024


def register_element(element_id: str, element: Any) -> str:
    """Bind an element to its id so actions do not have to walk the tree again."""
    if not isinstance(element_id, str) or not element_id.strip():
        raise ValueError("element_id must be a non-empty string")
    _ELEMENTS[element_id] = element
    while len(_ELEMENTS) > _MAX_REGISTRY:  # oldest first, never grow without bound
        _ELEMENTS.pop(next(iter(_ELEMENTS)))
    return element_id


def unregister_element(element_id: str) -> None:
    _ELEMENTS.pop(element_id, None)


def registered_ids() -> list[str]:
    return list(_ELEMENTS)


# --- id parsing and resolution ----------------------------------------------

_ID_SEPARATORS = re.compile(r"[,|;:\-.\s]+")
_WALK_NODE_BUDGET = 4096
_WALK_SECONDS_BUDGET = 3.0
_MAX_CHILDREN_PER_NODE = 256
_MAX_CHILDREN_PER_NODE = 512

# UIA pattern ids (stable across UIAutomationCore versions; also in the
# uiautomation package's PatternId class, inlined so this module works when
# the package is missing).
_PATTERN_IDS: dict[int, str] = {
    10000: "invoke",
    10005: "expand_collapse",
    10010: "select",
    10015: "toggle",
    10017: "scroll_item",
}

_TOGGLE_NAMES = {0: "off", 1: "on", 2: "indeterminate"}
_EXPAND_NAMES = {0: "collapsed", 1: "expanded", 2: "partially_expanded", 3: "leaf_node"}


def parse_element_id(element_id: Any) -> tuple[int, tuple[int, ...]]:
    """Split ``"hwnd:runtimeid"`` into ``(hwnd, runtime_id_tuple)``.

    Raises PatternError(INVALID_ID) for anything malformed.
    """
    if not isinstance(element_id, str) or ":" not in element_id:
        raise PatternError(ERR_INVALID_ID, f"element id must be 'hwnd:runtimeid', got {element_id!r}")
    raw_handle, _, raw_runtime = element_id.partition(":")
    try:
        hwnd = int(raw_handle.strip(), 10)
    except ValueError:
        raise PatternError(
            ERR_INVALID_ID, f"window handle {raw_handle!r} in {element_id!r} is not an integer"
        ) from None

    parts = [p for p in _ID_SEPARATORS.split(raw_runtime.strip()) if p]
    runtime: list[int] = []
    for part in parts:
        try:
            runtime.append(int(part, 0))
        except ValueError:
            raise PatternError(
                ERR_INVALID_ID,
                f"runtime id {raw_runtime!r} in {element_id!r} is not an int path",
            ) from None
    if not runtime:
        raise PatternError(ERR_INVALID_ID, f"element id {element_id!r} has an empty runtime id")
    return hwnd, tuple(runtime)


def resolve_element(element_id: Any) -> Any:
    """Return the UIA element for ``element_id``.

    Registry first (that is what snapshot/find published), then the
    ``"hwnd:runtimeid"`` parse plus a budgeted walk of the window subtree.
    A find-layer digest id resolves here without ever touching the parser.
    """
    if isinstance(element_id, str):
        element = _ELEMENTS.get(element_id)
        if element is not None:
            return element

    hwnd, runtime = parse_element_id(element_id)

    return _find_by_runtime_id(element_id, hwnd, runtime)


def _find_by_runtime_id(element_id: str, hwnd: int, runtime: tuple[int, ...]) -> Any:
    if _uia is None:
        raise PatternError(
            ERR_NOT_FOUND,
            f"{element_id!r} is not registered and uiautomation is not installed, "
            "so no lookup is possible",
        )
    try:
        root = _uia.ControlFromHandle(hwnd)
    except Exception as exc:  # dead window, bad handle, comtypes hiccup
        raise PatternError(
            ERR_NOT_FOUND, f"window handle {hwnd} did not resolve to a UIA element: {_summary(exc)}"
        ) from None
    if root is None:
        raise PatternError(ERR_NOT_FOUND, f"window handle {hwnd} has no UIA element")

    # The id carries the window handle plus the element path ("hwnd:7,321"),
    # so the match key is the full tuple (hwnd, *runtime). Bare-path
    # matching is deliberately NOT attempted: it would match ancestors
    # and siblings spuriously (and would defeat the node budget on
    # self-similar trees).
    found = _walk_for(root, (hwnd, *runtime), element_id)
    if found is not None:
        return found

    raise PatternError(
        ERR_NOT_FOUND, f"element {element_id!r} is not in the UIA tree of window {hwnd}"
    )


def _walk_for(root: Any, wanted: tuple[int, ...], element_id: str) -> Any:
    """Breadth-first search for a node whose runtime id equals ``wanted``.

    Node count and wall clock are both budgeted, checked as nodes are queued,
    so a lazy or pathological provider raises instead of hanging the act call.
    Returns None when the whole reachable tree was visited without a match.
    """
    deadline = time.monotonic() + _WALK_SECONDS_BUDGET
    seen: set[int] = {id(root)}
    queue: deque[Any] = deque([root])
    visited = 1

    def out_of_budget() -> bool:
        return visited > _WALK_NODE_BUDGET or time.monotonic() > deadline

    while queue:
        node = queue.popleft()
        if _runtime_id(node) == wanted:
            return node
        kids, truncated = _children(node)
        if truncated:
            raise PatternError(
                ERR_NOT_FOUND,
                f"node budget exhausted: a single node offered more than "
                f"{_MAX_CHILDREN_PER_NODE} children while looking for "
                f"{element_id!r}; exclude this window from traversal instead "
                "of scanning it",
            )
        for child in kids:
            if id(child) in seen:
                continue
            seen.add(id(child))
            queue.append(child)
            visited += 1
            if out_of_budget():
                # Pathological or slow tree: report skip, never hang.
                raise PatternError(
                    ERR_NOT_FOUND,
                    f"node budget exhausted after {_WALK_NODE_BUDGET} nodes / "
                    f"{_WALK_SECONDS_BUDGET}s while looking for {element_id!r}; "
                    "exclude this window from traversal instead of scanning it",
                )
    return None


def _runtime_id(node: Any) -> Optional[tuple[int, ...]]:
    raw = _safe_call(getattr(node, "GetRuntimeId", None))
    if not raw:
        return None
    try:
        return tuple(int(x) for x in raw)
    except (TypeError, ValueError):
        return None


def _children(node: Any) -> tuple[list[Any], bool]:
    """Children of ``node`` plus whether the provider offered MORE than the cap.

    The boolean is load-bearing: an unbounded (lazy) provider that overflows
    the per-node cap is pathological, and the walker must report a budget
    trip rather than silently visiting a truncated tree.
    """
    try:
        kids = node.GetChildren()
    except Exception:
        return [], False  # unreadable branch is a leaf for us, not a crash
    if not kids:
        return [], False
    if isinstance(kids, list):
        return kids[:_MAX_CHILDREN_PER_NODE], len(kids) > _MAX_CHILDREN_PER_NODE
    try:  # lazy sequence: never drain an unbounded generator
        probe = list(itertools.islice(iter(kids), _MAX_CHILDREN_PER_NODE + 1))
    except TypeError:
        return [], False
    if len(probe) > _MAX_CHILDREN_PER_NODE:
        return probe[:_MAX_CHILDREN_PER_NODE], True
    return probe, False


# --- snapshot ---------------------------------------------------------------

#: One shape for every action, so before/after diffs are comparable and the
#: transaction layer can log them verbatim.
SNAPSHOT_KEYS = (
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
)


def _safe_call(func: Any) -> Any:
    """Call a getter that may raise (stale element, bad UIPI), else None."""
    if not callable(func):
        return None
    try:
        return func()
    except Exception:
        return None


def _read(obj: Any, attr: str) -> Any:
    """Read a plain property, tolerating getters that raise."""
    try:
        return getattr(obj, attr, None)
    except Exception:
        return None


def _pattern_reader(getter_name: str, value_attr: str) -> Callable[[Any], Any]:
    def read(element: Any) -> Any:
        pattern = _safe_call(getattr(element, getter_name, None))
        if pattern is None:
            return None
        return _read(pattern, value_attr)

    return read


def _supported_patterns(element: Any) -> list[str]:
    names: list[str] = []
    for pattern_id, name in _PATTERN_IDS.items():
        if _safe_call(lambda pid=pattern_id: element.GetPattern(pid)) is not None:
            names.append(name)
    return names


def _enum_name(value: Any, table: dict[int, str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return table.get(int(value), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def snapshot(element: Any, element_id: Optional[str] = None) -> dict[str, Any]:
    """Best-effort, JSON-safe picture of an element.

    Never raises and never blocks: every property read is wrapped, and an
    unreadable property becomes None rather than an exception.
    """
    return {
        "element_id": element_id,
        "name": _read(element, "Name"),
        "control_type": _read(element, "ControlTypeName"),
        "automation_id": _read(element, "AutomationId"),
        "class_name": _read(element, "ClassName"),
        "enabled": _as_bool(_read(element, "IsEnabled")),
        "offscreen": _as_bool(_read(element, "IsOffscreen")),
        "focused": _as_bool(_read(element, "HasKeyboardFocus")),
        "toggle_state": _enum_name(
            _pattern_reader("GetTogglePattern", "ToggleState")(element), _TOGGLE_NAMES
        ),
        "is_selected": _as_bool(
            _pattern_reader("GetSelectionItemPattern", "IsSelected")(element)
        ),
        "expand_state": _enum_name(
            _pattern_reader("GetExpandCollapsePattern", "ExpandCollapseState")(element),
            _EXPAND_NAMES,
        ),
        "patterns": _supported_patterns(element),
    }


def _as_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


# --- action dispatch ---------------------------------------------------------


class _ActionSpec:
    __slots__ = ("pattern_getter", "method", "verify", "requires_enabled", "description")

    def __init__(
        self,
        pattern_getter: Optional[str],
        method: str,
        description: str,
        verify: Optional[str] = None,
        requires_enabled: bool = True,
    ) -> None:
        self.pattern_getter = pattern_getter  # None for focus (element itself)
        self.method = method
        self.description = description
        self.verify = verify
        self.requires_enabled = requires_enabled


_SPECS: dict[str, _ActionSpec] = {
    "invoke": _ActionSpec("GetInvokePattern", "Invoke", "InvokePattern"),
    "toggle": _ActionSpec("GetTogglePattern", "Toggle", "TogglePattern", verify="toggle"),
    "select": _ActionSpec(
        "GetSelectionItemPattern", "Select", "SelectionItemPattern", verify="select"
    ),
    "expand": _ActionSpec(
        "GetExpandCollapsePattern", "Expand", "ExpandCollapsePattern", verify="expand"
    ),
    "scroll_into_view": _ActionSpec(
        "GetScrollItemPattern", "ScrollIntoView", "ScrollItemPattern"
    ),
    "focus": _ActionSpec(
        None, "SetFocus", "IUIAutomationElement::SetFocus", verify="focus",
        requires_enabled=False,
    ),
}

ACTIONS = tuple(_SPECS)


def run_action(action: str, element_id: Any) -> ActionResult:
    """Run one pattern action and return before/after state.

    Never raises: every foreseeable failure comes back as an ActionResult with
    ``ok=False`` and a typed error code.
    """
    spec = _SPECS.get(action)
    known_id = element_id if isinstance(element_id, str) else ""

    if spec is None:
        return _result(
            action, known_id, {}, {}, PatternError(ERR_UNKNOWN_ACTION, f"unknown action {action!r}")
        )

    try:
        element = resolve_element(element_id)
    except PatternError as exc:
        return _result(action, known_id, {}, {}, exc)

    before = snapshot(element, known_id)
    if before["enabled"] is False and spec.requires_enabled:
        return _result(
            action,
            known_id,
            before,
            snapshot(element, known_id),
            PatternError(
                ERR_ELEMENT_DISABLED,
                f"element {known_id!r} is disabled, {spec.description} would be refused",
            ),
        )

    error, returned = _perform(element, spec)
    after = snapshot(element, known_id)

    if error is not None:
        return _result(action, known_id, before, after, error)

    if returned is False or _verify(spec, before, after) is False:
        message = (
            f"{spec.description} on {known_id!r} was called but the state says "
            "it did not take effect"
        )
        return _result(
            action, known_id, before, after, PatternError(ERR_ACTION_NO_EFFECT, message)
        )

    LOG.debug("%s %s -> %s", action, known_id, before.get("name"))
    return ActionResult(
        ok=True,
        action=action,
        element_id=known_id,
        before=before,
        after=after,
        error=None,
    )


def _perform(element: Any, spec: _ActionSpec) -> tuple[Optional[PatternError], Any]:
    """Call the pattern method. Returns (error, raw return value)."""
    try:
        pattern = element
        if spec.pattern_getter is not None:
            getter = getattr(element, spec.pattern_getter, None)
            if not callable(getter):
                raise PatternError(
                    ERR_PATTERN_UNSUPPORTED,
                    f"element does not expose {spec.pattern_getter}, "
                    f"so {spec.description} is not available",
                )
            pattern = getter()
        if pattern is None:  # uiautomation returns None for unsupported patterns
            raise PatternError(
                ERR_PATTERN_UNSUPPORTED, f"element does not support {spec.description}"
            )
        method = getattr(pattern, spec.method, None)
        if not callable(method):
            raise PatternError(
                ERR_PATTERN_UNSUPPORTED, f"{spec.description} has no callable {spec.method}"
            )
        return None, method()
    except PatternError as exc:
        return exc, None
    except Exception as exc:  # comtypes.COMError and friends
        return PatternError(*_classify(exc)), None


def _verify(spec: _ActionSpec, before: dict[str, Any], after: dict[str, Any]) -> Optional[bool]:
    """Did the state move? None means 'cannot tell', never a guess."""
    kind = spec.verify
    if kind == "toggle":
        was, now = before["toggle_state"], after["toggle_state"]
        if was is None or now is None:
            return None
        return was != now
    if kind == "select":
        now = after["is_selected"]
        return None if now is None else bool(now)
    if kind == "expand":
        now = after["expand_state"]
        if now is None:
            return None
        return now in ("expanded", "partially_expanded")
    if kind == "focus":
        now = after["focused"]
        return None if now is None else bool(now)
    return None  # invoke and scroll have nothing meaningful to compare


def _result(
    action: str,
    element_id: Any,
    before: dict[str, Any],
    after: dict[str, Any],
    error: PatternError,
) -> ActionResult:
    return ActionResult(
        ok=False,
        action=action,
        element_id=element_id,
        before=before,
        after=after,
        error=error.to_dict(),
    )


def _classify(exc: BaseException) -> tuple[str, str]:
    """Map a raw exception onto (code, message)."""
    hresult = _hresult(exc)
    if hresult == _HR_ACCESS_DENIED:
        return (
            ERR_ELEVATION_REQUIRED,
            "access denied sending this action (E_ACCESSDENIED); the target window is "
            "likely elevated, run the server as admin",
        )
    if hresult == _HR_ELEMENT_NOT_AVAILABLE:
        return (
            ERR_NOT_FOUND,
            "element is no longer available (UIA_E_ELEMENTNOTAVAILABLE); it was "
            "re-created or destroyed",
        )
    return ERR_PATTERN_FAILED, f"{type(exc).__name__}: {exc}"


def _hresult(exc: BaseException) -> Optional[int]:
    hresult = getattr(exc, "hresult", None)
    if isinstance(hresult, int):
        return hresult
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _summary(exc: BaseException) -> str:
    return _classify(exc)[1]


# --- TOOLS registry ----------------------------------------------------------


def _make_handler(action: str) -> Callable[..., dict]:
    def handler(element_id: Any = None, /, payload: Optional[dict] = None, **kwargs: Any) -> dict:
        if payload:
            kwargs.update(payload)
        if element_id is not None:
            kwargs.setdefault("id", element_id)
        args = ActionArgs.model_validate(kwargs)
        # run_action is the single dispatch point; it resolves through module
        # globals so a caller can swap it in one place.
        return run_action(action, args.id).model_dump(mode="json")

    handler.__name__ = f"{action}_handler"
    handler.__qualname__ = f"{action}_handler"
    return handler


handler_invoke = _make_handler("invoke")
handler_toggle = _make_handler("toggle")
handler_select = _make_handler("select")
handler_expand = _make_handler("expand")
handler_scroll_into_view = _make_handler("scroll_into_view")
handler_focus = _make_handler("focus")

#: Plain-name alias. Tools are also counted as leaf effects by patching
#: ``tools.patterns.invoke`` (same shape as ``tools.values.set_value``), so the
#: bare name has to exist on the module.
invoke = handler_invoke

TOOLS: list[PatternTool] = [
    PatternTool(
        name="invoke",
        description=(
            "Invoke an element through InvokePattern (button, menu item, "
            "hyperlink). Returns ok plus before/after state; no coordinates."
        ),
        schema=ActionArgs,
        handler=handler_invoke,
    ),
    PatternTool(
        name="toggle",
        description=(
            "Flip a check box / radio-like element through TogglePattern. "
            "Reports PATTERN_UNSUPPORTED when the element has no TogglePattern."
        ),
        schema=ActionArgs,
        handler=handler_toggle,
    ),
    PatternTool(
        name="select",
        description=(
            "Select a list item through SelectionItemPattern "
            "(list boxes, tabs, combo boxes)."
        ),
        schema=ActionArgs,
        handler=handler_select,
    ),
    PatternTool(
        name="expand",
        description=(
            "Expand an expandable element through ExpandCollapsePattern "
            "(tree nodes, combo box drop-downs). One direction only; there is "
            "no collapse tool in this set."
        ),
        schema=ActionArgs,
        handler=handler_expand,
    ),
    PatternTool(
        name="scroll_into_view",
        description=(
            "Scroll an element into view through ScrollItemPattern. Use it "
            "before acting on elements in long lists."
        ),
        schema=ActionArgs,
        handler=handler_scroll_into_view,
    ),
    PatternTool(
        name="focus",
        description=(
            "Give an element keyboard focus through IUIAutomationElement::"
            "SetFocus. The only action here that is not a control pattern, "
            "because focus is an element method, not a pattern."
        ),
        schema=ActionArgs,
        handler=handler_focus,
    ),
]

# This module deliberately does not call tools.register(TOOLS): the server
# calls tools.load_all_modules(), which imports and then registers every
# module. Self-registering here would make that a duplicate-name crash.
