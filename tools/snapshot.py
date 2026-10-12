"""snapshot tool: UIA tree capture, tree-first, no screenshots.

Task 3 of the implementation plan; spec §3, layer 1. Two modes:

- ``fast``  — the active window plus its direct children (two levels).
- ``full``  — every top-level window walked ``depth`` levels deep with an
  optional ``type_filter``, or the single subtree named by ``root``.

``full`` materializes each window's subtree through a UIA ``CacheRequest``
(properties + patterns + tree scope) and releases it in a ``finally``, so a
timeout or a dead element never leaks the request. Each window gets its own
sub-budget out of the call's total ``timeout_ms`` and its own exception
boundary, so a slow or broken provider is dropped and recorded in
``Snapshot.skipped`` by pid while the healthy windows still come back. The
released ``uiautomation`` builds ship that cached API commented out, so when
the module does not expose it the walk runs live with identical output,
budgets and semantics.

Every element carries a stable id, ``"hwnd:runtimeid"`` (the format the waits
layer parses), its canonical control kind, and the control patterns it
supports. The act layer resolves by pattern, never by coordinates.

Rules honoured here: ``uiautomation`` is optional at *import* time (this module
imports on a machine without it; calls then raise ``NotImplementedError``);
per-call traversal budgets are cooperative between UIA calls and cannot
interrupt a synchronous COM call already in progress; logging is stderr-only
via the logging module, and nothing is ever written to stdout, which the MCP
stdio transport owns. No images are taken, ever.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("win-mcp-pro.snapshot")
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

# ---------------------------------------------------------------------------
# budgets: a pathological provider must degrade, never hang the server
# ---------------------------------------------------------------------------

#: Levels captured counting the root as level 1 (depth 2 = root + children).
_MAX_DEPTH = 16
_MAX_CHILDREN = 512
_MAX_NODES = 4096
DEFAULT_TIMEOUT_MS = 5000
_MAX_TIMEOUT_MS = 600_000

# ---------------------------------------------------------------------------
# UIA ids (ABI-stable constants; the library's own values win when importable)
# ---------------------------------------------------------------------------

_NAME_PROP = 30005
_RUNTIME_ID_PROP = 30000
_AUTOMATION_ID_PROP = 30011
_CLASS_NAME_PROP = 30012
_BOUNDING_RECT_PROP = 30001

_INVOKE_PATTERN = 10000
_VALUE_PATTERN = 10002
_TOGGLE_PATTERN = 10015
_SELECTION_ITEM_PATTERN = 10010
_EXPAND_COLLAPSE_PATTERN = 10005
_SCROLL_ITEM_PATTERN = 10017
_SELECTION_PATTERN = 10001
_TEXT_PATTERN = 10014
_WINDOW_PATTERN = 10009

#: (friendly name, module constant, library attribute name) per probed pattern.
_WANTED_PATTERNS = (
    ("Invoke", _INVOKE_PATTERN, "InvokePattern"),
    ("Value", _VALUE_PATTERN, "ValuePattern"),
    ("Toggle", _TOGGLE_PATTERN, "TogglePattern"),
    ("Selection", _SELECTION_PATTERN, "SelectionPattern"),
    ("SelectionItem", _SELECTION_ITEM_PATTERN, "SelectionItemPattern"),
    ("ExpandCollapse", _EXPAND_COLLAPSE_PATTERN, "ExpandCollapsePattern"),
    ("ScrollItem", _SCROLL_ITEM_PATTERN, "ScrollItemPattern"),
    ("Text", _TEXT_PATTERN, "TextPattern"),
    ("Window", _WINDOW_PATTERN, "WindowPattern"),
)

#: Test/diagnostic seam: patch to control elapsed time without sleeping.
_perf_counter = time.perf_counter


# ---------------------------------------------------------------------------
# typed errors
# ---------------------------------------------------------------------------


class SnapshotTimeout(TimeoutError):
    """The per-call traversal budget ran out. Nodes are skipped, never retried."""

    code = "SNAPSHOT_TIMEOUT"


class RootNotFound(ValueError):
    """A ``root`` id did not resolve to a live element."""

    code = "ROOT_NOT_FOUND"


# ---------------------------------------------------------------------------
# pydantic v2 schemas
# ---------------------------------------------------------------------------


class SnapshotArgs(BaseModel):
    """Arguments of the ``snapshot`` tool (JSON Schema is exported from this)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["fast", "full"] = Field(
        default="fast",
        description="'fast': active window + direct children; 'full': subtree walk",
    )
    root: str | None = Field(
        default=None,
        description='element id "hwnd:runtimeid" to walk instead of the active window (full mode)',
    )
    depth: int = Field(
        default=2,
        ge=1,
        le=_MAX_DEPTH,
        description="levels captured counting the root as level 1 (full mode)",
    )
    type_filter: list[str] | None = Field(
        default=None,
        description="only capture these control kinds; 'Edit', 'edit' and 'EditControl' all match",
    )
    timeout_ms: int = Field(
        default=DEFAULT_TIMEOUT_MS,
        gt=0,
        le=_MAX_TIMEOUT_MS,
        description="traversal budget for this call; when it runs out the walk stops with SNAPSHOT_TIMEOUT",
    )


class SnapshotWindow(BaseModel):
    """One top-level window: enough to address it, nothing more."""

    hwnd: int
    title: str
    pid: int


class SnapshotElement(BaseModel):
    """One node of the captured tree.

    ``id`` is the stable ``"hwnd:runtimeid"`` string every other layer consumes;
    ``kind`` is the canonical control type without the ``Control`` suffix
    (``Edit``, ``Button``, ``Window``); ``patterns`` lists the control patterns
    the element supports, which is what the act layer calls.
    """

    id: str
    name: str
    kind: str
    patterns: list[str] = Field(default_factory=list)
    rect: list[int] | None = None


class Snapshot(BaseModel):
    """Result of one capture: the windows on screen and the captured elements.

    In ``full`` mode every top-level window is walked under its own sub-budget.
    A provider that raises or exhausts the cooperative budget is recorded in
    ``skipped``; a synchronous COM call already in progress cannot be
    interrupted. Excluded windows are skipped before traversal. Skips use pid
    or handle, never the window name.
    """

    windows: list[SnapshotWindow] = Field(default_factory=list)
    elements: list[SnapshotElement] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    skipped: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# control readers: work with uiautomation and with the test doubles alike
# ---------------------------------------------------------------------------


def _attr(control: Any, *names: str, default: Any = None) -> Any:
    """First non-None attribute among ``names``; a dead COM property is a miss."""
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


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_kind(value: Any) -> str:
    """'EditControl' / 'edit' -> 'edit' so every friendly spelling matches."""
    kind = _text(value).strip().lower()
    if kind.endswith("control"):
        kind = kind[: -len("control")]
    return kind.strip()


def _kind_of(control: Any) -> str:
    """Canonical control type: 'EditControl' / 'edit' -> 'Edit'."""
    raw = _attr(control, "ControlTypeName", "LocalizedControlType")
    return _norm_kind(raw).title() or "Unknown"


def _runtime_id_of(control: Any) -> list[int] | None:
    """Runtime id as ints, or None for a dead/unnamed element."""
    getter = getattr(control, "GetRuntimeId", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - stale element mid-walk
            return None
    else:
        value = _attr(control, "RuntimeId")
    if isinstance(value, (list, tuple)) and value:
        try:
            return [int(part) for part in value]
        except (TypeError, ValueError):
            return None
    return None


def _children_of(control: Any, cap: bool = True) -> list:
    """One hop down, capped; a stuck child never kills the walk.

    ``cap=False`` bypasses the per-node child cap for top-level window
    enumeration: windows are not tree children, so the traversal budget
    must never hide a whole window.
    """
    try:
        kids = control.GetChildren() or []
    except Exception as exc:  # noqa: BLE001 - a stuck child must not kill the walk
        log.warning("children query failed on %r: %s", control, exc)
        return []
    kids = list(kids)
    return kids[:_MAX_CHILDREN] if cap else kids


def _rect_of(control: Any) -> list[int] | None:
    rect = _attr(control, "BoundingRectangle")
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        try:
            return [int(round(float(v))) for v in rect]
        except (TypeError, ValueError):
            return None
    return None


def _hwnd_of(control: Any) -> int:
    return _int(_attr(control, "NativeWindowHandle"))


# ---------------------------------------------------------------------------
# exclusion (spec §5): a pathological provider is dropped before any traversal
# ---------------------------------------------------------------------------

#: Process names excluded from full-mode snapshot walks. Normalized on insert.
_EXCLUDED_PROCS: set[str] = set()


def exclude_proc(names: str | list[str] | tuple[str, ...] | set[str]) -> set[str]:
    """Exclude processes by executable name from full-mode snapshot walks.

    Accepts one name or an iterable of them; matching is case-insensitive on
    the lowercased name. This is the seam the failure museum registers into
    (``exclude_proc(["slowui.exe"])``), and it is also what the winproc slice
    exposes as a tool: when that slice is loaded, its registry is honoured here
    too, so one ``exclude_proc`` call keeps both tools consistent.
    """
    if isinstance(names, str):
        names = [names]
    for name in names:
        cleaned = _normalize_exe(name)
        if cleaned:
            _EXCLUDED_PROCS.add(cleaned)
    return _excluded_names()


def _normalize_exe(value: Any) -> str:
    """Normalize a process image to the same basename used by winproc."""
    name = _text(value).strip().replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _excluded_names() -> set[str]:
    """This module's exclusions merged with winproc's registry, when loaded."""
    names = set(_EXCLUDED_PROCS)
    winproc = sys.modules.get("tools.winproc")
    if winproc is not None:
        get_names = getattr(winproc, "excluded_names", None)
        extra = get_names() if callable(get_names) else getattr(winproc, "_EXCLUDE", ())
        if isinstance(extra, (set, frozenset, list, tuple)):
            names.update(extra)
    return {normalized for item in names if (normalized := _normalize_exe(item))}


def _process_names_by_pid(excluded_names: set[str]) -> dict[int, str]:
    """Resolve process IDs to executable basenames once per snapshot call."""
    if not excluded_names:
        return {}
    winproc = sys.modules.get("tools.winproc")
    enumerate_processes = getattr(winproc, "_process_snapshot", None)
    if not callable(enumerate_processes):
        return {}
    try:
        processes = enumerate_processes()
    except Exception as exc:  # noqa: BLE001 - exclusions must not break snapshots
        log.warning("process enumeration failed while applying exclusions: %s", exc)
        return {}
    return {
        pid: exe
        for process in processes
        if isinstance(process, dict)
        and (pid := _int(process.get("pid"))) > 0
        and (exe := _normalize_exe(process.get("exe")))
    }


def _is_excluded(
    control: Any,
    excluded_names: set[str],
    process_names_by_pid: dict[int, str],
) -> bool:
    """Match the UIA process ID to its executable, never its window title."""
    if not excluded_names:
        return False
    pid = _int(_attr(control, "ProcessId"))
    return bool(pid) and process_names_by_pid.get(pid, "") in excluded_names


def _skip_label(control: Any) -> str:
    """Identifier recorded in ``Snapshot.skipped`` for a dropped window.

    Deliberately not the window name: a skipped pathological provider must not
    leak its identity into the result (that is the contract the failure museum
    enforces). The pid or handle is enough for a human to look it up.
    """
    pid = _int(_attr(control, "ProcessId"))
    if pid:
        return f"pid:{pid}"
    return f"hwnd:{_hwnd_of(control)}"


def _pattern_id_of(constant: int, attr_name: str) -> int | None:
    """Library pattern id when importable, else the ABI constant."""
    pattern_ids = getattr(_uia, "PatternId", None) if _uia is not None else None
    value = getattr(pattern_ids, attr_name, None) if pattern_ids is not None else None
    return _int(value, constant) if value is not None else constant


def _property_id_of(attr_name: str, constant: int) -> int:
    property_ids = getattr(_uia, "PropertyId", None) if _uia is not None else None
    value = getattr(property_ids, attr_name, None) if property_ids is not None else None
    return _int(value, constant) if value is not None else constant


def _patterns_of(control: Any) -> list[str]:
    """Pattern names this control supports, in the order the act layer wants."""
    supported: list[str] = []
    for friendly, constant, attr_name in _WANTED_PATTERNS:
        pattern_id = _pattern_id_of(constant, attr_name)
        try:
            if control.GetPattern(pattern_id) is not None:
                supported.append(friendly)
        except Exception:  # noqa: BLE001 - unsupported or dead pattern
            continue
    return supported


def _element_id(hwnd: int, runtime_id: list[int]) -> str:
    """Stable id: the format the waits layer parses back apart."""
    return f"{hwnd}:{','.join(str(part) for part in runtime_id)}"


def _element_of(control: Any, hwnd: int, runtime_id: list[int]) -> SnapshotElement:
    return SnapshotElement(
        id=_element_id(hwnd, runtime_id),
        name=_text(_attr(control, "Name")),
        kind=_kind_of(control),
        patterns=_patterns_of(control),
        rect=_rect_of(control),
    )


# ---------------------------------------------------------------------------
# per-call budget
# ---------------------------------------------------------------------------


class _Budget:
    """Wall-clock budget shared by the whole call, checked before every node."""

    __slots__ = ("_start", "_deadline_s", "_mode")

    def __init__(self, timeout_ms: int, mode: str) -> None:
        self._start = _perf_counter()
        self._deadline_s = timeout_ms / 1000.0
        self._mode = mode

    def check(self) -> None:
        elapsed = _perf_counter() - self._start
        if elapsed > self._deadline_s:
            raise SnapshotTimeout(
                f"snapshot(mode={self._mode}) stopped after "
                f"{elapsed * 1000.0:.0f} ms, budget {self._deadline_s * 1000.0:.0f} ms"
            )


# ---------------------------------------------------------------------------
# cache request: props + patterns + scope, released in finally
# ---------------------------------------------------------------------------


def _cache_request() -> Any | None:
    """Build a UIA CacheRequest, or None when the library lacks the cached API.

    uiautomation 2.0.29 (the released build) ships ``CacheRequest`` commented
    out; newer builds expose it. Either way the walk below is the same code.
    """
    if _uia is None:
        return None
    cache_cls = getattr(_uia, "CacheRequest", None)
    if not callable(cache_cls):
        return None
    try:
        request = cache_cls()
    except Exception:  # noqa: BLE001 - a broken cache type is a missing one
        return None

    add_property = getattr(request, "AddProperty", None)
    if not callable(add_property):
        return None
    for attr_name, constant in (
        ("NameProperty", _NAME_PROP),
        ("RuntimeIdProperty", _RUNTIME_ID_PROP),
        ("AutomationIdProperty", _AUTOMATION_ID_PROP),
        ("ClassNameProperty", _CLASS_NAME_PROP),
        ("BoundingRectangleProperty", _BOUNDING_RECT_PROP),
    ):
        try:
            add_property(_property_id_of(attr_name, constant))
        except Exception:  # noqa: BLE001 - this property is not cacheable here
            continue

    add_pattern = getattr(request, "AddPattern", None)
    if callable(add_pattern):
        for _, constant, attr_name in _WANTED_PATTERNS:
            try:
                add_pattern(_pattern_id_of(constant, attr_name))
            except Exception:  # noqa: BLE001 - skip what cannot be cached
                continue

    scope = getattr(_uia, "TreeScope_Subtree", None)
    if scope is not None:
        try:
            request.TreeScope = scope
        except Exception:  # noqa: BLE001 - the default scope applies then
            pass
    mode = getattr(_uia, "AutomationElementMode_Full", None)
    if mode is not None:
        try:
            request.AutomationElementMode = mode
        except Exception:  # noqa: BLE001 - the default element mode applies then
            pass
    return request


def _release_cache(request: Any | None) -> None:
    """Release the cache request: explicit hook first, then drop the reference."""
    if request is None:
        return
    for method in ("Release", "Dispose"):
        release = getattr(request, method, None)
        if callable(release):
            try:
                release()
            except Exception:  # noqa: BLE001 - a failed hook is not fatal
                pass
            break


@contextlib.contextmanager
def _cached_tree(root_control: Any) -> Iterator[Any]:
    """Yield the subtree of ``root_control`` materialized through a cache.

    The request is built before the walk and released in ``finally`` so a
    timeout, a dead element or an exception never leaks it. Without a cached
    API the same control is yielded and the walk runs live.
    """
    request = _cache_request()
    build = getattr(root_control, "BuildUpdatedCache", None)
    if request is None or not callable(build):
        yield root_control
        return
    try:
        cached = build(request)
        yield cached if cached is not None else root_control
    finally:
        _release_cache(request)


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------


def _walk(root_control: Any, hwnd: int, type_filter: set[str] | None,
          budget: _Budget, depth: int) -> list[SnapshotElement]:
    """Depth-first walk of one subtree, document order, budgets enforced.

    ``depth`` counts the root as level 1, so 2 means root + direct children.
    A node whose runtime id cannot be read is skipped: UIA hands those out for
    dead elements, and guessing an id would poison every later lookup.
    """
    elements: list[SnapshotElement] = []
    visited: set[str] = set()
    nodes = 0

    def visit(control: Any, level: int) -> None:
        nonlocal nodes
        if control is None:
            return
        budget.check()
        nodes += 1
        if nodes > _MAX_NODES:
            log.debug("node budget reached (%d), stopping the walk", _MAX_NODES)
            return
        runtime_id = _runtime_id_of(control)
        if runtime_id is None:
            return
        element_id = _element_id(hwnd, runtime_id)
        if element_id in visited:
            return
        visited.add(element_id)
        element = _element_of(control, hwnd, runtime_id)
        if type_filter is None or element.kind in type_filter:
            elements.append(element)
        if level >= depth or nodes > _MAX_NODES:
            return
        for child in _children_of(control):
            visit(child, level + 1)

    visit(root_control, 1)
    return elements


def _list_windows() -> list:
    """Top-level windows, desktop order.

    This is the enumeration seam the server patches in tests; it returns
    controls, and identity reads (name/pid/handle) never descend, so a
    pathological provider costs nothing here.
    """
    if _uia is None:
        return []
    try:
        desktop = _uia.GetRootControl()
    except Exception:  # noqa: BLE001 - no desktop, no inventory
        return []
    return [c for c in _children_of(desktop, cap=False) if _kind_of(c) == "Window"]


def _window_info(control: Any) -> SnapshotWindow:
    """Identity of one top-level window: handle, title, pid."""
    return SnapshotWindow(
        hwnd=_hwnd_of(control),
        title=_text(_attr(control, "Name")),
        pid=_int(_attr(control, "ProcessId")),
    )


def _window_inventory() -> list[SnapshotWindow]:
    """Identity-only listing of every top-level window (no traversal)."""
    infos: list[SnapshotWindow] = []
    for control in _list_windows():
        hwnd = _hwnd_of(control)
        if not hwnd:
            continue
        infos.append(_window_info(control))
    return infos


def _active_window_control() -> Any | None:
    """The foreground control, promoted to its top-level window."""
    if _uia is None:
        return None
    control = None
    get_foreground = getattr(_uia, "GetForegroundControl", None)
    if callable(get_foreground):
        try:
            control = get_foreground()
        except Exception:  # noqa: BLE001 - treat as "no foreground"
            control = None
    if control is None:
        return None
    get_top = getattr(control, "GetTopLevelControl", None)
    if callable(get_top):
        try:
            top = get_top()
        except Exception:  # noqa: BLE001 - the control itself is the fallback
            top = None
        if top is not None:
            return top
    return control


def _split_id(element_id: str) -> tuple[str, str]:
    head, sep, tail = str(element_id).strip().partition(":")
    if not sep or not head.strip() or not tail.strip():
        raise RootNotFound(
            f"malformed element id {element_id!r}; expected 'hwnd:runtimeid'"
        )
    return head.strip(), tail.strip()


def _find_by_runtime_id(hwnd: int, runtime_id: str, budget: _Budget) -> Any | None:
    """Resolve 'hwnd:runtimeid' against the live tree of that window."""
    wanted = [part.strip() for part in runtime_id.split(",")]
    if not all(part.lstrip("-").isdigit() for part in wanted):
        return None

    def matches(control: Any) -> bool:
        found = _runtime_id_of(control)
        return found is not None and [str(p) for p in found] == wanted

    def search(control: Any, level: int) -> Any | None:
        budget.check()
        if matches(control):
            return control
        if level >= _MAX_DEPTH:
            return None
        for child in _children_of(control):
            found = search(child, level + 1)
            if found is not None:
                return found
        return None

    for window in _list_windows():
        if _hwnd_of(window) != hwnd:
            continue
        found = search(window, 1)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# the tool
# ---------------------------------------------------------------------------


def snapshot(
    mode: Literal["fast", "full"] = "fast",
    root: str | None = None,
    depth: int = 2,
    type_filter: list[str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> Snapshot:
    """Capture the UI tree as text.

    ``fast`` returns the active window and its direct children. ``full`` walks
    every top-level window (or the subtree of the element named by ``root``),
    at most ``depth`` levels each (the root counts as level 1), keeping only
    the kinds in ``type_filter`` when it is given.

    ``full`` isolates each window: its own sub-budget out of the call's total
    ``timeout_ms``, and its own exception boundary. A window that raises or
    exhausts its share between UIA calls is dropped and recorded in
    ``Snapshot.skipped`` by pid. The budget cannot interrupt a synchronous UIA
    or COM call that is already in progress. If the total budget is spent and
    nothing was captured, the call raises :class:`SnapshotTimeout` instead of
    returning an empty tree. An explicit ``root`` naming an excluded process
    returns empty elements with skip metadata and is never traversed; when
    every enumerated window is excluded, ``full`` returns empty instead of
    falling back to the active window.
    """
    args = SnapshotArgs(
        mode=mode, root=root, depth=depth,
        type_filter=type_filter, timeout_ms=timeout_ms,
    )
    if _uia is None:
        raise NotImplementedError(UNAVAILABLE_REASON)

    started = _perf_counter()
    kinds = _filter_kinds(args.type_filter)
    windows: list[SnapshotWindow] = []
    elements: list[SnapshotElement] = []
    skipped: list[str] = []
    timed_out = False

    if args.mode == "fast":
        # the active window plus its direct children, fixed depth
        window = _active_window_control()
        if window is None:
            log.debug("snapshot(fast): no foreground window to capture")
        else:
            windows = _window_inventory()  # identity-only: no traversal here
            elements = _walk(window, _hwnd_of(window), kinds,
                             _Budget(args.timeout_ms, "fast"), depth=2)
    elif args.root:
        # one scoped subtree: its own budget, its own failure boundary.
        # Exclusions apply here too: an explicitly named excluded root is
        # reported in skipped and never traversed (zero GetChildren).
        id_hwnd, runtime_id = _split_id(args.root)
        hwnd = _int(id_hwnd, 0)
        excluded_names = _excluded_names()
        process_names = _process_names_by_pid(excluded_names)
        if excluded_names:
            for window in _list_windows():
                if _hwnd_of(window) != hwnd:
                    continue
                if _is_excluded(window, excluded_names, process_names):
                    return Snapshot(
                        windows=[],
                        elements=[],
                        elapsed_ms=(_perf_counter() - started) * 1000.0,
                        skipped=[_skip_label(window)],
                    )
                break
        budget = _Budget(args.timeout_ms, "full")
        root_control = _find_by_runtime_id(hwnd, runtime_id, budget)
        if root_control is None:
            raise RootNotFound(
                f"no live element with id {args.root!r}; "
                "re-run snapshot to refresh ids"
            )
        if excluded_names and _is_excluded(root_control, excluded_names, process_names):
            return Snapshot(
                windows=[],
                elements=[],
                elapsed_ms=(_perf_counter() - started) * 1000.0,
                skipped=[_skip_label(root_control)],
            )
        windows = _window_inventory()
        try:
            with _cached_tree(root_control) as tree:
                elements = _walk(tree, hwnd, kinds, budget, args.depth)
        except SnapshotTimeout:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead root skips, never kills
            log.warning("root %r skipped: %s", args.root, exc)
            skipped.append(f"root:{args.root}")
    else:
        # every enumerated window, each behind its own sub-budget
        all_windows = _list_windows()
        excluded_names = _excluded_names()
        process_names = _process_names_by_pid(excluded_names)
        excluded = [
            control
            for control in all_windows
            if _is_excluded(control, excluded_names, process_names)
        ]
        pending = [
            control
            for control in all_windows
            if not _is_excluded(control, excluded_names, process_names)
        ]
        for control in excluded:
            skipped.append(_skip_label(control))  # never walked: exclusion first
        if not pending:
            if not all_windows:
                # No enumerated windows: try the foreground only when it is
                # not itself excluded; otherwise report skips and walk nothing.
                active = _active_window_control()
                if active is None:
                    return Snapshot(
                        windows=[],
                        elements=[],
                        elapsed_ms=(_perf_counter() - started) * 1000.0,
                        skipped=skipped,
                    )
                if _is_excluded(active, excluded_names, process_names):
                    skipped.append(_skip_label(active))
                    return Snapshot(
                        windows=[],
                        elements=[],
                        elapsed_ms=(_perf_counter() - started) * 1000.0,
                        skipped=skipped,
                    )
                pending = [active]
            else:
                # Every enumerated window was excluded: report the skips and
                # return empty metadata instead of falling back to an excluded
                # active window.
                return Snapshot(
                    windows=[],
                    elements=[],
                    elapsed_ms=(_perf_counter() - started) * 1000.0,
                    skipped=skipped,
                )
        left = max(1, len(pending))
        for index, control in enumerate(pending):
            hwnd = _hwnd_of(control)
            try:
                # The cache request is acquired before the budget check on
                # purpose: even an immediately-timed-out window must prove
                # the request was built and released (context manager).
                with _cached_tree(control) as tree:
                    remaining_ms = args.timeout_ms - (_perf_counter() - started) * 1000.0
                    if remaining_ms <= 0.0:
                        raise SnapshotTimeout(
                            f"snapshot(mode=full) out of budget before window "
                            f"{_skip_label(control)}"
                        )
                    sub_ms = remaining_ms / (left - index)
                    found = _walk(tree, hwnd, kinds, _Budget(sub_ms, "full"),
                                  args.depth)
            except SnapshotTimeout:
                timed_out = True
                skipped.append(_skip_label(control))  # skip this window, keep going
                continue
            except Exception as exc:  # noqa: BLE001 - a broken window is skipped
                log.warning("window %s skipped: %s", _skip_label(control), exc)
                skipped.append(_skip_label(control))
                continue
            elements.extend(found)
            windows.append(_window_info(control))
        if not elements and timed_out:
            raise SnapshotTimeout(
                f"snapshot(mode=full) produced nothing within "
                f"{args.timeout_ms} ms; {len(skipped)} window(s) skipped"
            )

    return Snapshot(
        windows=windows,
        elements=elements,
        elapsed_ms=(_perf_counter() - started) * 1000.0,
        skipped=skipped,
    )


def _filter_kinds(type_filter: list[str] | None) -> set[str] | None:
    """Normalize a type filter onto the canonical kind vocabulary."""
    if not type_filter:
        return None
    return {_norm_kind(kind).title() for kind in type_filter if _text(kind).strip()}


# ---------------------------------------------------------------------------
# tool registration (house contract: a TOOLS list of plain dicts)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "snapshot",
        "description": (
            "Capture the UI tree as text, no screenshots. mode='fast' returns the "
            "active window plus its direct children; mode='full' walks the subtree "
            "of the active window (or of the element named by root, an "
            "'hwnd:runtimeid' id) up to depth levels, optionally keeping only the "
            "control kinds in type_filter. Every element carries its stable "
            "'hwnd:runtimeid' id, control kind and supported control patterns, so "
            "find/act can address it without ever touching coordinates."
        ),
        "schema": SnapshotArgs,
        "handler": snapshot,
    }
]

__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "RootNotFound",
    "Snapshot",
    "SnapshotArgs",
    "SnapshotElement",
    "SnapshotTimeout",
    "SnapshotWindow",
    "TOOLS",
    "exclude_proc",
    "snapshot",
]
