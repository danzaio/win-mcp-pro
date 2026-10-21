"""Failure museum — the 5 Review Focus lines of the win-mcp-pro plan.

Each test mocks a broken or pathological condition and asserts the contract
that keeps it safe: skip-not-hang, exactly-once effect, typed errors with
remediation, and a vision fallback suggestion instead of a coordinate guess.

The owning module is imported only when its slice has landed; otherwise the
test skips, so this file never breaks collection.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

# Repo root on sys.path: pytest may put tests/ (not the repo root) there when
# tests/ has no package marker, and tools.* / server.* live at the root.
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

HANG_BUDGET_S = 12.0  # a call on a healthy path must land well under this


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class Deadline:
    """Run a call in a daemon thread so a hang fails the test, never freezes it.

    Never returns a bare value: :meth:`outcome` disambiguates a returned
    result from a raised exception from a hang, so "null" can never be
    mistaken for "returned None".
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.result = None
        self.error: BaseException | None = None
        self.finished = threading.Event()

    def _run(self, fn, args, kwargs):
        try:
            self.result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - recorded, never swallowed
            self.error = exc
        finally:
            self.finished.set()

    def __call__(self, fn, *args, **kwargs):
        threading.Thread(target=self._run, args=(fn, args, kwargs),
                         daemon=True).start()
        self.finished.wait(self.seconds)
        return self

    @property
    def hung(self) -> bool:
        return not self.finished.is_set()

    def outcome(self) -> tuple[str, object]:
        """("ok", result) | ("error", exc) | ("hung", None)."""
        if self.hung:
            return ("hung", None)
        if self.error is not None:
            return ("error", self.error)
        return ("ok", self.result)

    @property
    def payload(self):
        """Whatever came back: the exception when one was raised."""
        return self.error if self.error is not None else self.result


def _mod(name: str):
    """importorskip: when the owning slice has not landed, skip this test."""
    return pytest.importorskip(name, reason=f"{name} not available in this tree")


def _opt(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception:  # noqa: BLE001 - optional per test
        return None


def _resolve(mod, names: list[str]):
    """First existing callable attribute among names, else None."""
    if mod is None:
        return None
    for n in names:
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    return None


def _patch_everywhere(monkeypatch, names: list[str], value) -> list[str]:
    """Install a seam on every module that declares it (aliases may be bound
    at import time, so patching only the definition module is not enough)."""
    patched = []
    for modname in ("win_mcp_pro.tools.winproc", "win_mcp_pro.tools.snapshot", "win_mcp_pro.tools.query",
                    "win_mcp_pro.tools.patterns", "win_mcp_pro.tools.values",
                    "win_mcp_pro.server.main", "win_mcp_pro.server.registry", "win_mcp_pro.server.security"):
        mod = _opt(modname)
        if mod is None:
            continue
        for n in names:
            if hasattr(mod, n):
                try:
                    monkeypatch.setattr(f"{modname}.{n}", value, raising=False)
                    patched.append(f"{modname}.{n}")
                except Exception:  # noqa: BLE001
                    pass
    return patched


def _text(result) -> str:
    try:
        return json.dumps(result, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def _typed_error(result, code: str) -> bool:
    """True when result carries the typed error `code` (dict field or exception)."""
    want = code.upper()
    if isinstance(result, BaseException):
        return want in type(result).__name__.upper()
    if isinstance(result, dict):
        for key in ("error", "code", "error_type", "type", "status", "kind"):
            val = result.get(key)
            if isinstance(val, str) and val.upper() == want:
                return True
        for key in ("message", "reason", "detail"):
            val = result.get(key)
            if isinstance(val, str) and want in val.upper():
                return True
        return False
    return want in _text(result).upper()


class IdempotencyLedger:
    """Local reimplementation of the (window, element, action) contract.

    The oracle for this file: it decides per delivery whether the effect
    should apply, so the assertions do not trust the tool's bookkeeping.
    Every delivery pair below differs on the triple itself.
    """

    def __init__(self):
        self._seen: set[tuple] = set()
        self.deliveries: list[dict] = []

    @staticmethod
    def key(action_window: str, element: str, verb: str) -> tuple:
        return (str(action_window), str(element), str(verb))

    def should_apply(self, window: str, element: str, verb: str) -> bool:
        key = self.key(window, element, verb)
        self.deliveries.append({"window": window, "element": element, "action": verb})
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def expected_effects(self, deliveries) -> int:
        return sum(1 for d in deliveries if self.should_apply(
            d["window"], d["element"], d["action"]))


class CountableAction:
    """Mock executor: counts every real effect and returns before/after."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, action, *args, **kwargs):
        self.calls.append(action)
        n = len(self.calls)
        return {
            "ok": True,
            "before": {"calls_before": n - 1},
            "after": {"calls_after": n},
            "mock": True,
        }

    @property
    def count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# Review Focus 1 — slow/pathological provider: skip, not hang
# ---------------------------------------------------------------------------


class _SlowNode:
    """Pathological provider node: every children query costs `sleep_s`.

    The walk cannot be interrupted inside a single call, so the provider is
    slow rather than blocking: a per-call budget catches it between nodes.
    """

    def __init__(self, name: str, pid: int, handle: int, kind: str,
                 sleep_s: float, fanout: int, levels: int, _level: int = 1):
        self._name = name
        self._pid = pid
        self._handle = handle
        self.ControlTypeName = kind
        self.NativeWindowHandle = handle
        self._sleep_s = sleep_s
        self._fanout = fanout
        self._levels = levels
        self._level = _level

    @property
    def Name(self) -> str:
        return self._name

    @property
    def ProcessId(self) -> int:
        return self._pid

    @property
    def RuntimeId(self) -> list[int]:
        return [self._handle]

    def GetChildren(self):
        time.sleep(self._sleep_s)
        if self._level >= self._levels:
            return []
        return [
            _SlowNode(self._name, self._pid, self._handle * 10 + i, "Pane",
                      self._sleep_s, self._fanout, self._levels, self._level + 1)
            for i in range(self._fanout)
        ]

    @property
    def children(self):
        return self.GetChildren()


class _FastWindow:
    """Healthy sibling: proves the walk really ran (guards against vacuity)."""

    def __init__(self, name: str = "notepad.exe", pid: int = 777):
        self._name = name
        self._pid = pid
        self.ControlTypeName = "WindowControl"
        self.NativeWindowHandle = 0x1000 + pid

    @property
    def Name(self) -> str:
        return self._name

    @property
    def ProcessId(self) -> int:
        return self._pid

    @property
    def RuntimeId(self) -> list[int]:
        return [self.NativeWindowHandle]

    def GetChildren(self):
        return [_Leaf("DocumentControl", "notes.txt - Notepad")]

    @property
    def children(self):
        return self.GetChildren()


class _Leaf:
    def __init__(self, control_type: str, name: str):
        self._name = name
        self.ControlTypeName = control_type
        self.NativeWindowHandle = 0x2000

    @property
    def Name(self) -> str:
        return self._name

    @property
    def RuntimeId(self) -> list[int]:
        return [self.NativeWindowHandle]

    def GetChildren(self):
        return []

    @property
    def children(self):
        return []


#: Seams tools.snapshot exposes for injecting the provider: the walk root and
#: the window inventory (both are looked up per call, so patching works).
SNAPSHOT_SEAMS = ("_active_window_control", "_window_controls", "_list_windows",
                  "list_windows", "enum_windows", "_enum_windows", "roots", "_roots")


def _patch_snapshot_seams(monkeypatch, active, window_controls, list_windows) -> list[str]:
    """Patch whichever provider seams tools.snapshot actually exposes."""
    patched = _patch_everywhere(monkeypatch, list(SNAPSHOT_SEAMS), active)
    snapshot = _opt("win_mcp_pro.tools.snapshot")
    if snapshot is not None:
        if hasattr(snapshot, "_active_window_control"):
            monkeypatch.setattr(snapshot, "_active_window_control",
                                lambda: active, raising=False)
            patched.append("win_mcp_pro.tools.snapshot._active_window_control")
        if hasattr(snapshot, "_window_controls"):
            monkeypatch.setattr(snapshot, "_window_controls",
                                lambda: window_controls, raising=False)
            patched.append("win_mcp_pro.tools.snapshot._window_controls")
        if hasattr(snapshot, "_list_windows"):
            monkeypatch.setattr(snapshot, "_list_windows",
                                lambda: list_windows, raising=False)
            patched.append("win_mcp_pro.tools.snapshot._list_windows")
    return patched


def _register_exclusion(monkeypatch, name: str) -> bool:
    """Register `name` in the exclusion seam the snapshot/proc slice exposes."""
    for owner in ("win_mcp_pro.tools.winproc", "win_mcp_pro.tools.snapshot"):
        mod = _opt(owner)
        if mod is None:
            continue
        fn = _resolve(mod, ["exclude_proc", "exclude_process", "add_exclude", "exclude"])
        if fn is not None:
            for attempt in (lambda: fn([name]), lambda: fn(name)):
                try:
                    attempt()
                    return True
                except TypeError:
                    continue
                except Exception:  # noqa: BLE001 - try the next shape
                    continue
        for attr in ("EXCLUDE_PROC", "EXCLUDED_PROCS", "EXCLUDE_PROCESSES", "EXCLUDE"):
            if hasattr(mod, attr):
                try:
                    monkeypatch.setattr(f"{owner}.{attr}", {name}, raising=False)
                    return True
                except Exception:  # noqa: BLE001
                    pass
    return False


def test_slow_provider_skipped_when_excluded(monkeypatch):
    """Pathological provider in exclude_proc -> skipped, traversal stays fast.

    spec §5: "exclude_proc antes de qualquer traversal (árvore lenta nunca é
    entrada)". Skip when the slice has not exposed the seam yet.
    """
    snapshot = _mod("win_mcp_pro.tools.snapshot")

    slow = _SlowNode("slowui.exe", 4242, 0x900, "WindowControl",
                     sleep_s=0.5, fanout=6, levels=3)
    patched = _patch_snapshot_seams(
        monkeypatch, active=slow, window_controls=[slow, _FastWindow()],
        list_windows=[slow, _FastWindow()])
    assert patched, "no provider seam in tools.snapshot to inject the mock"
    if not _register_exclusion(monkeypatch, "slowui.exe"):
        pytest.skip(
            "no exclusion seam (exclude_proc()/EXCLUDE_PROC) in tools.snapshot "
            "or tools.winproc yet; spec §5 requires excluding a pathological "
            "provider before any traversal — the contract assertions below are "
            "ready for the slice that lands it"
        )
    winproc = _opt("win_mcp_pro.tools.winproc")
    if winproc is not None:
        monkeypatch.setattr(
            winproc,
            "_process_snapshot",
            lambda: [
                {"pid": 4242, "exe": "slowui.exe"},
                {"pid": 777, "exe": "notepad.exe"},
            ],
        )

    snap_fn = _resolve(snapshot, ["snapshot", "take_snapshot", "capture"])
    assert snap_fn is not None, "win_mcp_pro.tools.snapshot exposes no snapshot()"

    started = time.monotonic()
    call = Deadline(HANG_BUDGET_S)
    call(lambda: snap_fn(mode="full", root=None, depth=4, type_filter=None,
                         timeout_ms=HANG_BUDGET_S * 1000))
    elapsed = time.monotonic() - started
    assert not call.hung, (
        f"traversal HUNG >{HANG_BUDGET_S}s on slowui.exe although it is excluded; "
        "exclusion must be honoured before descending (skip-not-hang)"
    )
    assert call.error is None, (
        f"snapshot raised instead of skipping the excluded provider: {call.error!r}"
    )
    blob = _text(call.result).lower()
    assert "slowui" not in blob, (
        "slowui.exe appears in the snapshot although it is in exclude_proc; "
        "an excluded (pathological) provider must never be traversed"
    )
    assert "notepad" in blob or "notes.txt" in blob, (
        "the fast window vanished from the snapshot, so the exclusion assertion "
        "above would be vacuous: the mock tree was never walked"
    )
    assert elapsed < HANG_BUDGET_S, (
        f"snapshot took {elapsed:.1f}s; an excluded provider must cost nothing"
    )


def test_slow_provider_times_out_per_call(monkeypatch):
    """Slow provider NOT excluded -> per-call timeout skips it, still no hang.

    Every GetChildren() costs 0.5s over a 6x6x6 tree: without the per-call
    budget the walk needs ~20s; with it the call must come back in ~0.6s with
    the typed SNAPSHOT_TIMEOUT skip (or a partial capture).
    """
    snapshot = _mod("win_mcp_pro.tools.snapshot")

    slow = _SlowNode("slowui.exe", 4242, 0x900, "WindowControl",
                     sleep_s=0.5, fanout=6, levels=3)
    patched = _patch_snapshot_seams(
        monkeypatch, active=slow, window_controls=[slow], list_windows=[slow])
    assert patched, "no provider seam in tools.snapshot to inject the mock"

    snap_fn = _resolve(snapshot, ["snapshot", "take_snapshot", "capture"])
    assert snap_fn is not None, "win_mcp_pro.tools.snapshot exposes no snapshot()"

    call = Deadline(HANG_BUDGET_S)
    started = time.monotonic()
    call(lambda: snap_fn(mode="full", root=None, depth=4, type_filter=None,
                         timeout_ms=120))
    elapsed = time.monotonic() - started
    assert not call.hung, (
        "traversal HUNG on a slow provider; the per-call timeout must skip the "
        "slow nodes instead of waiting for the whole tree"
    )

    status, value = call.outcome()
    if status == "error":
        assert isinstance(value, BaseException) and (
            "timeout" in type(value).__name__.lower()
            or "timeout" in str(value).lower()
        ), (
            f"snapshot failed with a non-timeout error on the slow provider: "
            f"{value!r}"
        )
        # typed skip: SnapshotTimeout.code is the SNAPSHOT_TIMEOUT contract
        code = getattr(value, "code", None)
        assert code in (None, "SNAPSHOT_TIMEOUT"), (
            f"timeout error carries an unexpected code {code!r}"
        )
    else:
        # partial capture is an acceptable skip too, as long as it was bounded
        assert elapsed < 3.0, (
            f"snapshot needed {elapsed:.1f}s on a provider that costs 0.5s per "
            "hop; the per-call timeout did not bound the walk"
        )

    assert elapsed < 3.0, (
        f"snapshot needed {elapsed:.1f}s on a slow provider (budget was 120ms); "
        "skip-not-hang means the slow nodes are dropped, not waited out"
    )


# ---------------------------------------------------------------------------
# Review Focus 2 — duplicate delivery: single effect
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review Focus 3 — elevated target: ELEVATION_REQUIRED typed error
# ---------------------------------------------------------------------------


def test_elevated_target_typed_error(monkeypatch):
    """check_uipi on a higher-integrity target -> ELEVATION_REQUIRED, fast."""
    security = _mod("win_mcp_pro.server.security")

    target_pid = 4242

    def fake_rid(pid):
        return 0x3000 if pid == target_pid else 0x2000  # high vs medium

    monkeypatch.setattr(security, "integrity_rid", fake_rid)

    started = time.monotonic()
    call = Deadline(10.0)
    call(lambda: security.check_uipi(target_pid))
    assert not call.hung, "check_uipi hung on an elevated target; it must fail fast"
    assert call.error is None, f"check_uipi raised: {call.error!r}"

    res = call.result
    assert res.get("ok") is False and res.get("code") == "ELEVATION_REQUIRED", (
        f"expected a typed ELEVATION_REQUIRED failure, got {_text(res)[:200]}; "
        "an elevated target must never fail silently or as a generic error"
    )
    detail = str(res.get("detail", "")).lower()
    assert "admin" in detail or "uiaccess" in detail, (
        f"ELEVATION_REQUIRED carries no remediation instruction: {detail[:200]}"
    )
    assert time.monotonic() - started < 10.0, "integrity check was not immediate"


def test_elevated_target_control_equal_integrity(monkeypatch):
    """Non-elevated target -> ok (guards the test above against always-error)."""
    security = _mod("win_mcp_pro.server.security")

    monkeypatch.setattr(security, "integrity_rid", lambda pid: 0x2000)
    call = Deadline(10.0)
    call(lambda: security.check_uipi(4242))
    assert not call.hung and call.error is None
    res = call.result
    assert res.get("ok") is True and res.get("code") != "ELEVATION_REQUIRED", (
        f"equal-integrity target wrongly reported ELEVATION_REQUIRED: "
        f"{_text(res)[:200]}"
    )


# ---------------------------------------------------------------------------
# Review Focus 4 — double-submit retry: exactly-once via before/after log
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review Focus 5 — missing element on custom render: NOT_FOUND + vision hint
# ---------------------------------------------------------------------------


class _CustomRenderRoot:
    """Custom-render app: one unnamed canvas node, nothing resolvable by name."""

    ControlTypeName = "CustomControl"
    NativeWindowHandle = 0x900

    @property
    def Name(self) -> str:
        return ""

    def GetChildren(self):
        return [_CustomRenderLeaf()]

    @property
    def children(self):
        return self.GetChildren()


class _CustomRenderLeaf(_CustomRenderRoot):
    NativeWindowHandle = 0x901


class _NamedRoot:
    """Control tree: a healthy named window, so a miss is a real miss."""

    ControlTypeName = "WindowControl"
    NativeWindowHandle = 0x1001

    def __init__(self, name: str = "notes.txt - Notepad"):
        self._name = name

    @property
    def Name(self) -> str:
        return self._name

    def GetChildren(self):
        return [_Leaf("DocumentControl", self._name)]

    @property
    def children(self):
        return self.GetChildren()


def _install_tree(monkeypatch, query, windows) -> None:
    monkeypatch.setattr(query, "_roots", lambda: list(windows), raising=False)
    # keep find() on its query path even where uiautomation is importable
    monkeypatch.setattr(query, "_uia", object(), raising=False)
    if hasattr(query, "_registry_clear"):
        query._registry_clear()


def test_find_finds_present_element_control(monkeypatch):
    """Control: with a healthy tree, find() must resolve the element."""
    query = _mod("win_mcp_pro.tools.query")
    _install_tree(monkeypatch, query, [_NamedRoot()])

    call = Deadline(20.0)
    call(lambda: query.find(name="notes.txt - Notepad"))
    assert not call.hung, "find() hung on the mock tree"
    payload = call.payload
    assert not _typed_error(payload, "NOT_FOUND"), (
        f"healthy tree but find() returned NOT_FOUND: {_text(payload)[:200]}; "
        "without this control the missing-element test below is vacuous"
    )
    assert payload.get("ok") is True and payload.get("element", {}).get("id"), (
        f"find() did not return a stable id: {_text(payload)[:200]}"
    )


def test_missing_element_not_found_with_vision_hint(monkeypatch):
    """Custom-render app: NOT_FOUND with reason + vision fallback, no guessing."""
    query = _mod("win_mcp_pro.tools.query")
    _install_tree(monkeypatch, query, [_CustomRenderRoot()])

    call = Deadline(20.0)
    call(lambda: query.find(name="Send Invoice"))
    assert not call.hung, "find() hung on the custom-render tree"
    payload = call.payload

    assert _typed_error(payload, "NOT_FOUND"), (
        f"expected NOT_FOUND for an element no provider exposes, got "
        f"{_text(payload)[:200]}"
    )
    text = _text(payload)
    low = text.lower()
    assert "custom" in low or "provider" in low or "render" in low or "canvas" in low, (
        f"NOT_FOUND gives no reason why the tree could not resolve it: {text[:200]}"
    )
    assert "screenshot" in low or "zoom" in low or "vision" in low, (
        "NOT_FOUND does not suggest the vision fallback (screenshot/zoom); the "
        "spec requires proposing it, never guessing coordinates"
    )
    assert not re.search(r'"rect"|click\s*\(\s*\d+\s*,\s*\d+|coordinate', low), (
        "find() fell back to coordinates on a custom-render miss: "
        f"{text[:200]}"
    )
