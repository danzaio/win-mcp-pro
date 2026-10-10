"""win-mcp-pro transactions + audit (Task 11).

Four MCP tools plus the state other tool modules share:

- ``preview(action)``     describe target and effect, execute nothing,
                          mint a single-use ``preview_id`` for irreversible work
- ``dry_run(action)``     full pipeline walk-through with no side effect:
                          reports dedupe hits and missing previews instead of running
- ``before_after(action_id)``  read the ring log of the last 50 executed actions
                          with their before/after state (retry evidence)
- ``profile(action)``     per-stage timing plus ``tokens_saved_est`` against a
                          screenshot-based vision baseline

Irreversible actions (delete, send, submit, pay, or anything whose ``args``
declare ``irreversible: true``) require a fresh ``preview_id`` for the same
``(window, element, action, args)``; otherwise the call fails with
``PREVIEW_REQUIRED``. ``preview_id`` is consumed on use.

Other tool modules wire in like this::

    from tools import transact

    def handler_send(action: transact.ActionIn) -> dict:
        return transact.execute_action(action, executor=send_message)

    transact.register_executor("send", send_message)   # for profile(execute=True)

``check_and_mark`` is the bare helper for modules that keep their own
executor: mark first, execute on a miss, record the result::

    if transact.check_and_mark(action)["deduped"]:
        return {"ok": True, "deduped": True}
    result = executor(action)
    return transact.record_result(action, result)

No screenshots anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

__all__ = [
    "TOOLS",
    "ActionIn",
    "ProfileIn",
    "IRREVERSIBLE_ACTIONS",
    "PREVIEW_TTL",
    "DEDUPE_WINDOW_S",
    "RING_SIZE",
    "VISION_BASELINE_TOKENS",
    "UIA_TOKENS_PER_ACTION",
    "IdempotencyRegistry",
    "REGISTRY",
    "EXECUTORS",
    "action_key",
    "args_hash",
    "is_irreversible",
    "register_executor",
    "check_and_mark",
    "preview",
    "preview_gate",
    "dry_run",
    "execute_action",
    "run",
    "profile",
    "record_action",
    "record_result",
    "before_after",
    "recent",
    "reset",
]

# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------

#: Actions that must not run without a prior preview (spec §5).
IRREVERSIBLE_ACTIONS = frozenset({"delete", "send", "submit", "pay"})

#: How long a preview_id stays valid, in seconds.
PREVIEW_TTL = 300

#: Duplicate (window, element, action, args) inside this window is deduped.
DEDUPE_WINDOW_S = 60.0

#: before_after ring log depth.
RING_SIZE = 50

#: Registry caps (the process can be long-lived; state must stay bounded).
MAX_PREVIEWS = 256
MAX_REGISTRY_ENTRIES = 2048

#: Token accounting for profile(): a screenshot round-trip costs roughly this
#: many tokens (1920x1080 png, scaled by the model), the UIA tree roughly the
#: second number. The difference is what profile reports as saved.
VISION_BASELINE_TOKENS = 1200
UIA_TOKENS_PER_ACTION = 100

# ---------------------------------------------------------------------------
# schemas (pydantic v2)
# ---------------------------------------------------------------------------


class ActionIn(BaseModel):
    """One UI action plus the identity used for idempotency."""

    window: str = Field(min_length=1, description="Window title, hwnd, or process name.")
    element: str = Field(min_length=1, description="Stable element id (hwnd:runtimeid) or Name.")
    action: str = Field(min_length=1, description="Tool/action verb, e.g. invoke, set_value, send.")
    args: dict[str, Any] = Field(default_factory=dict, description="Action arguments.")
    preview_id: str | None = Field(default=None, description="From preview(); required for irreversible actions.")


class ProfileIn(ActionIn):
    """profile() input: same action, plus whether to really execute it."""

    execute: bool = Field(default=False, description="Run the registered executor while profiling.")


class BeforeAfterIn(BaseModel):
    """before_after input."""

    action_id: str = Field(min_length=1, description="action_id returned by the executed action.")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _monotonic() -> float:
    return time.monotonic()


def _ms_since(start: float) -> float:
    return round((_monotonic() - start) * 1000.0, 3)


def _ms_since_at(start: float, now: float) -> int:
    return max(0, int((now - start) * 1000))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _err(code: str, reason: str) -> dict:
    return {"ok": False, "error": code, "reason": reason, "before": None, "after": None}


def args_hash(args: dict[str, Any]) -> str:
    """Stable hash of the args mapping, insensitive to key order."""
    blob = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _as_action(action: Any) -> ActionIn:
    """House convention: every entry accepts a model or a plain mapping."""
    if isinstance(action, dict):
        return ActionIn(**action)
    return action


def action_key(action: ActionIn) -> tuple[str, str, str, str]:
    """Idempotency key: (window, element, action, args-hash)."""
    action = _as_action(action)
    return (action.window, action.element, action.action, args_hash(action.args))


def is_irreversible(action: ActionIn) -> bool:
    verb = action.action.strip().lower()
    if verb in IRREVERSIBLE_ACTIONS:
        return True
    return bool((action.args or {}).get("irreversible", False))


def _describe_effect(action: ActionIn) -> str:
    verb = action.action.strip().lower()
    effects = {
        "invoke": "press the control",
        "set_value": f"replace the value with {action.args.get('text')!r}",
        "get_value": "read the value (no change)",
        "toggle": "flip the check state",
        "select": "select the item in its container",
        "expand": "expand the node",
        "focus": "move keyboard focus",
        "paste_text": f"paste {action.args.get('text')!r} from the clipboard",
        "send": "send the message",
        "submit": "submit the form",
        "delete": f"delete {action.args.get('target')!r}",
        "pay": f"pay {action.args.get('amount')!r}",
    }
    if verb in effects:
        return f"{action.action}: {effects[verb]} on '{action.element}' in '{action.window}'"
    return f"{action.action}: run on '{action.element}' in '{action.window}'"


# ---------------------------------------------------------------------------
# idempotency registry
# ---------------------------------------------------------------------------


class IdempotencyRegistry:
    """Seen-keys for ``DEDUPE_WINDOW_S``: the second identical action is a
    replay, not a new one. Entries expire, and the map stays bounded."""

    def __init__(self, window_s: float = DEDUPE_WINDOW_S, max_entries: int = MAX_REGISTRY_ENTRIES):
        self.window_s = window_s
        self.max_entries = max_entries
        self._seen: dict[tuple[str, str, str, str], float] = {}
        self._lock = threading.RLock()

    def _sweep(self, now: float) -> None:
        stale = [key for key, at in self._seen.items() if now - at > self.window_s]
        for key in stale:
            del self._seen[key]

    def _trim(self) -> None:
        while len(self._seen) > self.max_entries:
            oldest = next(iter(self._seen))
            del self._seen[oldest]

    def check_and_mark(self, key: tuple[str, str, str, str], now: float | None = None) -> dict:
        """First sight inside the window marks the key; the rest are deduped."""
        stamp = _monotonic() if now is None else now
        with self._lock:
            self._sweep(stamp)
            seen_at = self._seen.get(key)
            if seen_at is not None and stamp - seen_at <= self.window_s:
                return {"deduped": True, "age_s": round(stamp - seen_at, 3)}
            self._seen[key] = stamp
            self._trim()
            return {"deduped": False, "age_s": 0.0}

    def peek(self, key: tuple[str, str, str, str], now: float | None = None) -> dict:
        """Would this key dedupe right now? No marking, no side effect."""
        stamp = _monotonic() if now is None else now
        with self._lock:
            self._sweep(stamp)
            seen_at = self._seen.get(key)
            if seen_at is not None and stamp - seen_at <= self.window_s:
                return {"deduped": True, "age_s": round(stamp - seen_at, 3)}
            return {"deduped": False, "age_s": 0.0}

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


REGISTRY = IdempotencyRegistry()

#: Executors other modules register so profile(execute=True) can time them.
EXECUTORS: dict[str, Callable[[ActionIn], dict]] = {}
_EXECUTORS_LOCK = threading.RLock()


def register_executor(action: str, fn: Callable[[ActionIn], dict]) -> None:
    """Register the real executor of ``action`` (used by profile only)."""
    with _EXECUTORS_LOCK:
        EXECUTORS[action.strip().lower()] = fn


def get_executor(action: str) -> Callable[[ActionIn], dict] | None:
    with _EXECUTORS_LOCK:
        return EXECUTORS.get(action.strip().lower())


# ---------------------------------------------------------------------------
# previews
# ---------------------------------------------------------------------------

#: preview_id -> (key, created_monotonic). Bounded by MAX_PREVIEWS.
_PREVIEWS: dict[str, tuple[tuple[str, str, str, str], float]] = {}
_PREVIEWS_LOCK = threading.RLock()


def _preview_status(action: ActionIn, now: float | None = None) -> tuple[str, str]:
    """("not_required"|"ok"|"PREVIEW_REQUIRED", reason). No consumption."""
    if not is_irreversible(action):
        return "not_required", ""
    if not action.preview_id:
        return "PREVIEW_REQUIRED", (
            f"'{action.action}' is irreversible: call preview() first and pass "
            "the returned preview_id"
        )
    stamp = _monotonic() if now is None else now
    with _PREVIEWS_LOCK:
        entry = _PREVIEWS.get(action.preview_id)
        if entry is None:
            return "PREVIEW_REQUIRED", "preview_id is unknown or already used"
        key, created = entry
        if key != action_key(action):
            return "PREVIEW_REQUIRED", "preview_id was issued for a different action"
        if stamp - created > PREVIEW_TTL:
            del _PREVIEWS[action.preview_id]
            return "PREVIEW_REQUIRED", f"preview expired after {PREVIEW_TTL}s"
    return "ok", ""


def preview_gate(action: ActionIn, now: float | None = None) -> dict | None:
    """Block irreversible actions without a matching preview. Consumes the
    preview_id on success. Returns None when the action may run."""
    action = _as_action(action)
    state, reason = _preview_status(action, now=now)
    if state == "ok":
        with _PREVIEWS_LOCK:
            _PREVIEWS.pop(action.preview_id, None)
        return None
    if state == "not_required":
        return None
    return _err("PREVIEW_REQUIRED", reason)


def preview(action: ActionIn) -> dict:
    """Describe target and effect without executing. Irreversible actions get
    back a single-use preview_id for the real call."""
    action = _as_action(action)
    key = action_key(action)
    with _PREVIEWS_LOCK:
        if len(_PREVIEWS) >= MAX_PREVIEWS:
            _PREVIEWS.pop(next(iter(_PREVIEWS)), None)
        preview_id = "pv_" + uuid.uuid4().hex[:8]
        while preview_id in _PREVIEWS:
            preview_id = "pv_" + uuid.uuid4().hex[:8]
        _PREVIEWS[preview_id] = (key, _monotonic())
    irreversible = is_irreversible(action)
    return {
        "ok": True,
        "preview_id": preview_id,
        "target": {"window": action.window, "element": action.element},
        "action": action.action,
        "args": action.args,
        "effect": _describe_effect(action),
        "reversible": not irreversible,
        "irreversible": irreversible,
        "confirm_required": irreversible,
    }


# ---------------------------------------------------------------------------
# before/after ring log
# ---------------------------------------------------------------------------

_RING: deque[dict] = deque(maxlen=RING_SIZE)
_RING_INDEX: dict[str, dict] = {}
_RING_LOCK = threading.RLock()


def record_action(
    action: ActionIn,
    before: Any = None,
    after: Any = None,
    ok: bool = True,
    error: str | None = None,
    reason: str | None = None,
    deduped: bool = False,
    elapsed_ms: int | None = None,
) -> str:
    """Append one entry to the ring log and return its action_id."""
    action = _as_action(action)
    action_id = "act_" + uuid.uuid4().hex[:10]
    entry = {
        "action_id": action_id,
        "ts": _utc_now(),
        "window": action.window,
        "element": action.element,
        "action": action.action,
        "args_hash": args_hash(action.args),
        "ok": ok,
        "error": error,
        "reason": reason,
        "deduped": deduped,
        "elapsed_ms": elapsed_ms,
        "before": before,
        "after": after,
    }
    with _RING_LOCK:
        if len(_RING) == _RING.maxlen:
            _RING_INDEX.pop(_RING[0]["action_id"], None)
        _RING.append(entry)
        _RING_INDEX[action_id] = entry
    return action_id


def record_result(action: ActionIn, result: dict, elapsed_ms: int | None = None) -> dict:
    """Record an ActionResult a module executed by itself; keep its fields."""
    result = dict(result)
    action_id = record_action(
        action,
        before=result.get("before"),
        after=result.get("after"),
        ok=bool(result.get("ok", True)),
        error=result.get("error"),
        reason=result.get("reason"),
        elapsed_ms=elapsed_ms,
    )
    result["action_id"] = action_id
    return result


def recent(limit: int = RING_SIZE) -> list[dict]:
    """Newest-last copies of the last ``limit`` ring entries."""
    with _RING_LOCK:
        items = list(_RING)[-max(0, limit):]
        return [dict(entry) for entry in items]


def before_after(action_id: str) -> dict:
    """One recorded action, or a typed NOT_FOUND."""
    with _RING_LOCK:
        entry = _RING_INDEX.get(action_id)
        found = dict(entry) if entry is not None else None
    if found is None:
        return {
            "ok": False,
            "error": "NOT_FOUND",
            "reason": (
                f"no recorded action with id '{action_id}' "
                f"(ring holds the last {RING_SIZE} actions)"
            ),
        }
    return {"ok": True, "entry": found}


# ---------------------------------------------------------------------------
# the transactional path
# ---------------------------------------------------------------------------


def check_and_mark(action_or_key, now: float | None = None) -> dict:
    """Helper for tool modules: ``{"deduped": True}`` when this exact
    ``(window, element, action, args)`` already ran inside the window."""
    key = action_or_key if isinstance(action_or_key, tuple) else action_key(_as_action(action_or_key))
    return REGISTRY.check_and_mark(key, now=now)


def _as_result(value: Any) -> dict:
    """Accept an ActionResult dict or pydantic model from any executor."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    return {"ok": True, "value": value}


def _execute(action: ActionIn, executor: Callable[[ActionIn], dict], now: float | None = None) -> dict:
    """Preview gate, idempotency mark, execution, ring log. One call."""
    if executor is None or not callable(executor):
        return _err("BAD_REQUEST", "execute_action needs a callable executor")
    action = _as_action(action)
    started = _monotonic()

    gate = preview_gate(action, now=now)
    if gate is not None:
        stamp = _monotonic() if now is None else now
        action_id = record_action(
            action, ok=False, error=gate["error"], reason=gate["reason"],
            elapsed_ms=_ms_since_at(started, stamp),
        )
        return {**gate, "action_id": action_id}

    key = action_key(action)
    dedupe = REGISTRY.check_and_mark(key, now=now)
    if dedupe["deduped"]:
        stamp = _monotonic() if now is None else now
        action_id = record_action(
            action, ok=True, deduped=True,
            elapsed_ms=_ms_since_at(started, stamp),
        )
        return {"ok": True, "deduped": True, "before": None, "after": None, "action_id": action_id}

    try:
        result = _as_result(executor(action))
    except Exception as exc:  # COM errors, torn-down controls, tool bugs
        message = f"{type(exc).__name__}: {exc}"[:300]
        action_id = record_action(
            action, ok=False, error="EXECUTOR_ERROR", reason=message,
            elapsed_ms=_ms_since(started),
        )
        return {
            "ok": False, "error": "EXECUTOR_ERROR", "reason": message,
            "before": None, "after": None, "action_id": action_id,
        }

    recorded = record_result(action, result, elapsed_ms=_ms_since(started))
    recorded.setdefault("deduped", False)
    return recorded


def execute_action(action: ActionIn, executor: Callable[[ActionIn], dict], now: float | None = None) -> dict:
    """Run one action through the transaction with an explicit executor."""
    return _execute(action, executor, now=now)


#: Value-style verbs that live in tools.values; everything else is a pattern
#: action handled by tools.patterns.run_action.
_LEAF_VALUE_VERBS = frozenset({"set_value", "get_value", "paste_text"})


def _leaf_dispatch(action: ActionIn) -> dict:
    """Default executor: route the verb to the real leaf tool. Resolved at
    call time so tests (and the server) can swap the leaf underneath us."""
    verb = action.action.strip().lower()
    if verb in _LEAF_VALUE_VERBS:
        try:
            from tools import values as _values
        except ImportError as exc:
            return {"ok": False, "error": "NO_EXECUTOR",
                    "reason": f"tools.values unavailable: {exc}"}
        fn = getattr(_values, verb, None)
        if not callable(fn):
            return {"ok": False, "error": "NO_EXECUTOR",
                    "reason": f"tools.values exposes no {verb}()"}
        if verb == "get_value":
            return _as_result(fn(action.element))
        return _as_result(fn(action.element, str(action.args.get("text", ""))))
    try:
        from tools import patterns as _patterns
    except ImportError as exc:
        return {"ok": False, "error": "NO_EXECUTOR",
                "reason": f"tools.patterns unavailable: {exc}"}
    fn = getattr(_patterns, "run_action", None)
    if not callable(fn):
        return {"ok": False, "error": "NO_EXECUTOR",
                "reason": "tools.patterns exposes no run_action()"}
    return _as_result(fn(verb, action.element))


def _default_executor(action: ActionIn) -> Callable[[ActionIn], dict]:
    fn = get_executor(action.action)
    return fn if fn is not None else _leaf_dispatch


def run(action: ActionIn, executor: Callable[[ActionIn], dict] | None = None,
        now: float | None = None) -> dict:
    """Public entry for one action: model or mapping in, transactional
    pipeline out. Without an explicit executor the verb is routed to the real
    leaf tool (or a registered one via register_executor)."""
    action = _as_action(action)
    if executor is None:
        executor = _default_executor(action)
    return _execute(action, executor, now=now)


def dry_run(action: ActionIn, now: float | None = None) -> dict:
    """Walk the whole pipeline without touching anything: would this execute,
    and if not, why. Never marks the registry, never consumes a preview."""
    action = _as_action(action)
    key = action_key(action)
    dedupe = REGISTRY.peek(key, now=now)
    state, reason = _preview_status(action, now=now)
    preview_required = state == "PREVIEW_REQUIRED"

    blocked_by = None
    if dedupe["deduped"]:
        blocked_by = "DEDUPE"
    elif preview_required:
        blocked_by = "PREVIEW_REQUIRED"

    return {
        "ok": True,
        "performed": False,
        "would_execute": blocked_by is None,
        "blocked_by": blocked_by,
        "deduped": dedupe["deduped"],
        "preview_required": preview_required,
        "preview_state": state,
        "reason": reason,
        "effect": _describe_effect(action),
        "reversible": not is_irreversible(action),
        "key": list(key),
    }


def profile(action: ActionIn, executor: Callable[[ActionIn], dict] | None = None,
            now: float | None = None) -> dict:
    """Per-stage timing plus the token estimate. Reads only: nothing is marked
    and no preview is consumed. Pass ``execute=True`` (ProfileIn) with a
    registered or injected executor to time a real run."""
    if isinstance(action, dict):
        action = ProfileIn(**action)
    started = _monotonic()
    stamp = (lambda: now) if now is not None else _monotonic
    stages: dict[str, float] = {}

    mark = stamp()
    action_key(action)
    _describe_effect(action)
    stages["preview"] = _ms_since_at(mark, stamp())

    mark = _monotonic()
    state, reason = _preview_status(action, now=now)
    stages["guard"] = _ms_since(mark)

    mark = _monotonic()
    dedupe = REGISTRY.peek(action_key(action), now=now)
    stages["dedupe"] = _ms_since(mark)

    stages["execute"] = 0.0
    blocked_by = None
    if dedupe["deduped"]:
        blocked_by = "DEDUPE"
    elif state == "PREVIEW_REQUIRED":
        blocked_by = "PREVIEW_REQUIRED"

    want_execute = bool(getattr(action, "execute", False))
    result = None
    if want_execute and blocked_by is None:
        fn = executor or get_executor(action.action)
        if fn is None:
            return {
                "ok": False,
                "error": "UNKNOWN_ACTION",
                "reason": f"no executor registered for action '{action.action}'",
                "stages_ms": stages,
                "total_ms": _ms_since(started),
            }
        mark = _monotonic()
        try:
            result = dict(fn(action))
            stages["execute"] = _ms_since(mark)
        except Exception as exc:
            stages["execute"] = _ms_since(mark)
            result = {"ok": False, "error": "EXECUTOR_ERROR",
                      "reason": f"{type(exc).__name__}: {exc}"[:300]}

    saved = VISION_BASELINE_TOKENS - UIA_TOKENS_PER_ACTION
    payload = {
        "ok": True,
        "executed": result is not None,
        "blocked_by": blocked_by,
        "reason": reason if blocked_by == "PREVIEW_REQUIRED" else None,
        "stages_ms": stages,
        "total_ms": _ms_since(started),
        "tokens": {
            "vision_baseline": VISION_BASELINE_TOKENS,
            "uia_tree": UIA_TOKENS_PER_ACTION,
            "saved_est": saved,
            "saved_pct": round(saved / VISION_BASELINE_TOKENS * 100),
        },
        "tokens_saved_est": saved,
    }
    if result is not None:
        payload["result"] = result
    return payload


# ---------------------------------------------------------------------------
# handler wrappers: accept either a mapping or keyword arguments
# ---------------------------------------------------------------------------


def _merge(params: dict | None, kwargs: dict) -> dict:
    data = dict(params or {})
    data.update(kwargs)
    return data


def _bad_request(reason: str) -> dict:
    return {"ok": False, "error": "BAD_REQUEST", "reason": reason}


def _handle_preview(params: dict | None = None, **kwargs: Any) -> dict:
    try:
        parsed = ActionIn(**_merge(params, kwargs))
    except ValidationError as exc:
        return _bad_request(f"invalid arguments: {exc.errors()[0]['msg']}")
    return preview(parsed)


def _handle_dry_run(params: dict | None = None, **kwargs: Any) -> dict:
    try:
        parsed = ActionIn(**_merge(params, kwargs))
    except ValidationError as exc:
        return _bad_request(f"invalid arguments: {exc.errors()[0]['msg']}")
    return dry_run(parsed)


def _handle_before_after(params: dict | None = None, **kwargs: Any) -> dict:
    try:
        parsed = BeforeAfterIn(**_merge(params, kwargs))
    except ValidationError as exc:
        return _bad_request(f"invalid arguments: {exc.errors()[0]['msg']}")
    return before_after(parsed.action_id)


def _handle_profile(params: dict | None = None, **kwargs: Any) -> dict:
    try:
        parsed = ProfileIn(**_merge(params, kwargs))
    except ValidationError as exc:
        return _bad_request(f"invalid arguments: {exc.errors()[0]['msg']}")
    return profile(parsed)


# ---------------------------------------------------------------------------
# tool registry contract: TOOLS: list[{name, description, schema, handler}]
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "preview",
        "description": (
            "Describe the target and the effect of an action without executing "
            "it. Irreversible actions (delete, send, submit, pay, or args with "
            "irreversible=true) come back with a single-use preview_id that the "
            "real call must pass, otherwise it fails with PREVIEW_REQUIRED."
        ),
        "schema": ActionIn,
        "handler": _handle_preview,
    },
    {
        "name": "dry_run",
        "description": (
            "Walk the full transaction pipeline without side effects and report "
            "what would happen: deduped inside the 60s window, missing or stale "
            "preview for irreversible actions, or clear to execute."
        ),
        "schema": ActionIn,
        "handler": _handle_dry_run,
    },
    {
        "name": "before_after",
        "description": (
            "Read the audit entry of one executed action (action_id from its "
            "result): before/after state, ok, error, timing. Ring log of the "
            "last 50 actions; this is the evidence that a retried submit ran "
            "exactly once."
        ),
        "schema": BeforeAfterIn,
        "handler": _handle_before_after,
    },
    {
        "name": "profile",
        "description": (
            "Time an action per stage (preview, guard, dedupe, execute) and "
            "estimate tokens saved versus a screenshot/vision baseline. Reads "
            "only; pass execute=true with a registered executor to time a real "
            "run end to end."
        ),
        "schema": ProfileIn,
        "handler": _handle_profile,
    },
]


# ---------------------------------------------------------------------------
# state reset (tests and server restart)
# ---------------------------------------------------------------------------


def reset() -> None:
    """Drop every piece of process state: registry, ring, previews, executors."""
    REGISTRY.clear()
    with _PREVIEWS_LOCK:
        _PREVIEWS.clear()
    with _RING_LOCK:
        _RING.clear()
        _RING_INDEX.clear()
    with _EXECUTORS_LOCK:
        EXECUTORS.clear()
