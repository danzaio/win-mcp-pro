"""win-mcp-pro — single-call polling waits (Task 8).

Five tools, all polling INSIDE one call (no client-side loops):

* ``wait(ms)``               — plain bounded sleep, returns ``{ok, elapsed_ms}``.
* ``wait_text(s, id, t)``    — until the element's Name contains ``s``.
* ``wait_focus(id, t)``      — until the element has keyboard focus.
* ``wait_enabled(id, t)``    — until the element is enabled.
* ``wait_gone(id, t)``       — until the element no longer exists.

Element ids resolve through the shared registries first (``find`` /
``dump_tree`` publish there) and fall back to the canonical
``"hwnd:runtimeid"`` walk through ``tools.patterns``. Every handler observes
the element the id names -- never the window root -- and ``wait_focus``
compares the focused control's RuntimeId with the target's, failing closed
when either identity cannot be read.

Every handler returns ``{"ok": bool, "elapsed_ms": int}``. A wait never
raises because of a provider hiccup: the condition is retried until the
deadline, and ``ok=False`` means "still not true when time ran out".

Import-safety: ``uiautomation`` is Windows-only and may be missing in
CI/test environments, so it is imported lazily behind ``_load_uia()``.
Tests replace ``_provider`` with a fake; the real provider only runs
on a Windows box with the package installed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# provider layer (swappable for tests)
# ---------------------------------------------------------------------------


class WaitsProvider:
    """Protocol waits.py talks to. The default impl resolves real ids."""

    def resolve_element(self, element_id: str) -> Any:
        """The live control the id names, or None when nothing resolves."""
        raise NotImplementedError

    def get_focus(self) -> Any:
        raise NotImplementedError

    def element_text(self, element: Any) -> str:
        raise NotImplementedError

    def element_enabled(self, element: Any) -> bool:
        raise NotImplementedError

    def element_exists(self, element: Any) -> bool:
        raise NotImplementedError


def _load_uia() -> Any:
    """Import uiautomation lazily; raise a clear error when absent."""
    try:
        import uiautomation  # type: ignore
    except Exception as exc:  # ImportError, comtypes issues, non-Windows...
        raise RuntimeError(
            "uiautomation is not available (Windows-only dependency)"
        ) from exc
    return uiautomation


class UiaWaitsProvider(WaitsProvider):
    """Real provider: shared registries first, then the budgeted walk.

    ``uiautomation`` is imported on first use, never at module import, so
    this module stays importable on machines without it. Resolution goes
    through ``tools.patterns.resolve_element``: the in-process registries
    answer for ids that ``find``/``dump_tree`` published, and anything else
    is resolved by a budgeted walk of the canonical ``"hwnd:runtimeid"``
    form. A controlled not-found comes back as None; unexpected failures
    bubble up to the poller, which treats them as "not yet" instead of
    reporting success.
    """

    def resolve_element(self, element_id: str) -> Any | None:
        from tools import patterns

        try:
            return patterns.resolve_element(element_id)
        except patterns.PatternError:
            return None

    def get_focus(self) -> Any:
        uia = _load_uia()
        return uia.GetFocusControl()

    def element_text(self, element: Any) -> str:
        try:
            return element.GetName() or ""
        except Exception:
            return ""

    def element_enabled(self, element: Any) -> bool:
        return bool(element.GetEnabled())

    def element_exists(self, element: Any) -> bool:
        return bool(element.Exists())


_provider: WaitsProvider = UiaWaitsProvider()

# ---------------------------------------------------------------------------
# timing knobs (tests override _POLL_INTERVAL_MS)
# ---------------------------------------------------------------------------

_POLL_INTERVAL_MS = 100          # ~10 polls/second, one call per MCP turn
_MAX_POLLS = 1000                # hard iteration cap: never spin forever
_MAX_TIMEOUT_MS = 60_000         # 60s ceiling per wait call


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[..., Any]


# ---------------------------------------------------------------------------
# schemas (pydantic v2)
# ---------------------------------------------------------------------------

_ELEMENT_ID = r"(?:(?:0x[0-9a-fA-F]+|\d+):.+|[0-9a-f]{12})"
_ID_DESCRIPTION = (
    'Element id from find/snapshot: "hwnd:runtimeid" (e.g. "17524:42,8,2") '
    "or a 12-hex registry id."
)


class WaitArgs(BaseModel):
    ms: int = Field(default=0, ge=0, description="How long to sleep, in ms.")


class WaitTextArgs(BaseModel):
    s: str = Field(min_length=1, description="Substring to wait for in the element Name.")
    id: str = Field(pattern=f"^{_ELEMENT_ID}$", description=_ID_DESCRIPTION)
    timeout_ms: int = Field(default=5000, ge=0, description="Give up after this many ms.")


class WaitIdArgs(BaseModel):
    id: str = Field(pattern=f"^{_ELEMENT_ID}$", description=_ID_DESCRIPTION)
    timeout_ms: int = Field(default=5000, ge=0, description="Give up after this many ms.")


# Friendly aliases (same shape, one name per tool).
WaitFocusArgs = WaitEnabledArgs = WaitGoneArgs = WaitIdArgs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _resolve(element_id: str) -> Any | None:
    """The element an id names, or None when nothing resolves under it."""
    return _provider.resolve_element(element_id)


def _runtime_id_of(element: Any) -> tuple[int, ...] | None:
    """Runtime id as an int tuple, or None when it cannot be read."""
    getter = getattr(element, "GetRuntimeId", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - dead element mid-poll
            return None
    else:
        try:
            value = getattr(element, "RuntimeId", None)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (list, tuple)) and value:
        try:
            return tuple(int(part) for part in value)
        except (TypeError, ValueError):
            return None
    return None


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _poll(condition: Callable[[], bool], timeout_ms: int) -> dict:
    """Run ``condition`` until it is true, the deadline hits, or the
    iteration cap is reached. One MCP call, bounded wall time."""
    timeout_ms = min(max(0, timeout_ms), _MAX_TIMEOUT_MS)
    start = time.monotonic()
    deadline = start + timeout_ms / 1000.0
    polls = 0
    while True:
        try:
            if condition():
                return {"ok": True, "elapsed_ms": _elapsed_ms(start)}
        except Exception:
            # provider blew up mid-wait (element torn down, COM error...):
            # treat as "not yet" and keep polling until the deadline.
            pass
        polls += 1
        if polls >= _MAX_POLLS:
            return {"ok": False, "elapsed_ms": _elapsed_ms(start)}
        now = time.monotonic()
        if now >= deadline:
            return {"ok": False, "elapsed_ms": _elapsed_ms(start)}
        time.sleep(min(_POLL_INTERVAL_MS / 1000.0, max(0.0, deadline - now)))


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def handler_wait(ms: int = 0) -> dict:
    """Bounded sleep. Returns ok=True always (it is a sleep, not a state)."""
    ms = min(max(0, ms), _MAX_TIMEOUT_MS)
    start = time.monotonic()
    time.sleep(ms / 1000.0)
    return {"ok": True, "elapsed_ms": _elapsed_ms(start)}


def handler_wait_text(s: str, id: str, timeout_ms: int = 5000) -> dict:
    wanted = s.casefold()

    def condition() -> bool:
        element = _resolve(id)
        if element is None:
            return False
        return wanted in (element_text := _provider.element_text(element)).casefold()

    return _poll(condition, timeout_ms)


def handler_wait_focus(id: str, timeout_ms: int = 5000) -> dict:
    def condition() -> bool:
        target = _resolve(id)
        if target is None:
            return False
        focused = _provider.get_focus()
        if focused is None:
            return False
        target_runtime = _runtime_id_of(target)
        focused_runtime = _runtime_id_of(focused)
        if target_runtime is None or focused_runtime is None:
            # Identity that cannot be proven must never report success:
            # keep polling and let the deadline decide.
            return False
        return target_runtime == focused_runtime

    return _poll(condition, timeout_ms)


def handler_wait_enabled(id: str, timeout_ms: int = 5000) -> dict:
    def condition() -> bool:
        element = _resolve(id)
        if element is None:
            return False
        return _provider.element_enabled(element)

    return _poll(condition, timeout_ms)


def handler_wait_gone(id: str, timeout_ms: int = 5000) -> dict:
    def condition() -> bool:
        element = _resolve(id)
        if element is None:
            return True
        return not _provider.element_exists(element)

    return _poll(condition, timeout_ms)


# ---------------------------------------------------------------------------
# tool registry contract
# ---------------------------------------------------------------------------

TOOLS = [
    ToolDef(
        name="wait",
        description=(
            "Sleep for a bounded number of milliseconds. Plain pacing between "
            "actions; never a substitute for waiting on state."
        ),
        schema=WaitArgs,
        handler=handler_wait,
    ),
    ToolDef(
        name="wait_text",
        description=(
            "Wait (inside this call) until the element's Name contains the given "
            "text. Returns ok=False on timeout."
        ),
        schema=WaitTextArgs,
        handler=handler_wait_text,
    ),
    ToolDef(
        name="wait_focus",
        description=(
            "Wait until the element receives keyboard focus. Useful after invoke "
            "on editors and dialogs."
        ),
        schema=WaitIdArgs,
        handler=handler_wait_focus,
    ),
    ToolDef(
        name="wait_enabled",
        description=(
            "Wait until the element becomes enabled. Covers 'button greyed out "
            "while the app works'."
        ),
        schema=WaitIdArgs,
        handler=handler_wait_enabled,
    ),
    ToolDef(
        name="wait_gone",
        description=(
            "Wait until the element no longer exists. Covers spinners, toasts "
            "and 'busy' overlays disappearing."
        ),
        schema=WaitIdArgs,
        handler=handler_wait_gone,
    ),
]
