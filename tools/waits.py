"""win-mcp-pro — single-call polling waits (Task 8).

Five tools, all polling INSIDE one call (no client-side loops):

* ``wait(ms)``               — plain bounded sleep, returns ``{ok, elapsed_ms}``.
* ``wait_text(s, id, t)``    — until the element's Name contains ``s``.
* ``wait_focus(id, t)``      — until the element has keyboard focus.
* ``wait_enabled(id, t)``    — until the element is enabled.
* ``wait_gone(id, t)``       — until the element no longer exists.
* ``wait_change``            — until a screen region's pixels change.

``wait_change`` watches pixels (loading spinners, progress bars, canvas
updates the tree cannot describe); the rest watch the accessibility tree.

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

from pydantic import BaseModel, Field, model_validator

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
            return str(element.Name or "")
        except Exception:
            pass
        getter = getattr(element, "GetName", None)
        if callable(getter):
            try:
                return getter() or ""
            except Exception:
                return ""
        return ""

    def element_enabled(self, element: Any) -> bool:
        try:
            return bool(element.IsEnabled)
        except Exception:
            pass
        getter = getattr(element, "GetEnabled", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                return False
        return False

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


class WaitChangeArgs(BaseModel):
    monitor: int = Field(default=0, ge=0, description="Zero-based display index.")
    left: int | None = Field(default=None, ge=0, description="Region start (all four or none).")
    top: int | None = Field(default=None, ge=0)
    right: int | None = Field(default=None, gt=0)
    bottom: int | None = Field(default=None, gt=0)
    threshold: float = Field(
        default=0.02, ge=0.0, le=1.0,
        description="Fraction of (downscaled) pixels that must differ from "
                    "the baseline before the wait fires.",
    )
    timeout_ms: int = Field(default=5000, ge=0, description="Give up after this many ms.")

    @model_validator(mode="after")
    def _complete_region(self) -> "WaitChangeArgs":
        given = [value is not None for value in (self.left, self.top, self.right, self.bottom)]
        if any(given) and not all(given):
            raise ValueError("give left/top/right/bottom together, or none for the full monitor")
        return self


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


#: Downscale cap for pixel diffs: a full 4K frame is ~25M pixels, but a
#: spinner or a relabeled button flips whole blocks, so 160px across keeps
#: the per-poll diff at a few thousand pixels and still sees real change.
_DIFF_MAX_WIDTH = 160
#: Per-pixel luminance distance that counts as "different" (of 255).
_DIFF_TOLERANCE = 12


def _gray_small(rgb: bytes, width: int, height: int,
               region: tuple[int, int, int, int] | None) -> bytes:
    """Downscaled grayscale bytes of the (optionally cropped) capture."""
    from PIL import Image

    image = Image.frombytes("RGB", (width, height), rgb)
    if region is not None:
        image = image.crop(region)
    gray = image.convert("L")
    width, height = gray.size
    scale = min(1.0, _DIFF_MAX_WIDTH / max(width, 1))
    if scale < 1.0:
        gray = gray.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    return gray.tobytes()


def _fraction_changed(baseline: bytes, current: bytes) -> float:
    different = sum(1 for old, new in zip(baseline, current)
                    if abs(old - new) > _DIFF_TOLERANCE)
    return different / max(1, len(baseline))


def handler_wait_change(args: Any) -> dict:
    """Wait (inside this call) until a screen region's pixels change.

    Captures the baseline on entry, then polls downscaled grayscale frames
    until the fraction of changed pixels exceeds ``threshold``. Returns
    ``{"ok", "elapsed_ms", "changed"}``; a capture failure mid-wait is
    "not yet" (same philosophy as the tree waits), never success.
    """
    from tools import vision

    m = WaitChangeArgs(**dict(args or {}))
    try:
        rgb, width, height, _origin = vision._capture(m.monitor)
    except ImportError:
        return {"ok": False, "error": "UNAVAILABLE",
                "reason": "vision backend unavailable (mss/pillow not installed)",
                "elapsed_ms": 0, "changed": 0.0}
    except ValueError as exc:
        return {"ok": False, "error": "BAD_MONITOR", "reason": str(exc)[:200],
                "elapsed_ms": 0, "changed": 0.0}
    except Exception as exc:  # noqa: BLE001 - capture failure is data
        return {"ok": False, "error": "CAPTURE_FAILED", "reason": str(exc)[:200],
                "elapsed_ms": 0, "changed": 0.0}
    if m.left is None:
        region = None
    else:
        try:
            vision._validate_region(m.left, m.top, m.right, m.bottom, width, height)
        except ValueError as exc:
            return {"ok": False, "error": "BAD_REGION", "reason": str(exc)[:200],
                    "elapsed_ms": 0, "changed": 0.0}
        region = (m.left, m.top, min(m.right, width), min(m.bottom, height))
    try:
        baseline = _gray_small(rgb, width, height, region)
    except ImportError:
        return {"ok": False, "error": "UNAVAILABLE",
                "reason": "vision backend unavailable (mss/pillow not installed)",
                "elapsed_ms": 0, "changed": 0.0}
    timeout_ms = min(max(0, m.timeout_ms), _MAX_TIMEOUT_MS)
    start = time.monotonic()
    deadline = start + timeout_ms / 1000.0
    polls = 0
    changed = 0.0
    while True:
        try:
            rgb, width, height, _origin = vision._capture(m.monitor)
            changed = _fraction_changed(baseline, _gray_small(rgb, width, height, region))
            if changed > m.threshold:
                return {"ok": True, "elapsed_ms": _elapsed_ms(start),
                        "changed": round(changed, 4)}
        except Exception:
            # torn-down display, racing resolution change...: not yet.
            pass
        polls += 1
        if polls >= _MAX_POLLS:
            return {"ok": False, "elapsed_ms": _elapsed_ms(start),
                    "changed": round(changed, 4)}
        now = time.monotonic()
        if now >= deadline:
            return {"ok": False, "elapsed_ms": _elapsed_ms(start),
                    "changed": round(changed, 4)}
        time.sleep(min(_POLL_INTERVAL_MS / 1000.0, max(0.0, deadline - now)))


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
    ToolDef(
        name="wait_change",
        description=(
            "Wait (inside this call) until a screen region's pixels change "
            "past the threshold fraction. Covers loading spinners, progress "
            "bars and canvas updates the tree cannot describe. Returns "
            "ok/elapsed_ms/changed; ok=False on timeout."
        ),
        schema=WaitChangeArgs,
        handler=handler_wait_change,
    ),
]
