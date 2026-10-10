"""Tests for tools.snapshot.

Covers: fast-mode shape (active window + direct children only), stable ids in
the "hwnd:runtimeid" format, full-mode subtree walk (CacheRequest when the lib
exposes it, live fallback otherwise), per-call timeout with release, type
filter, root-id resolution, traversal budgets, and the missing-library
degradation. All UIA access goes through a fake ``uiautomation`` module, so
nothing here touches the real desktop.
"""
import re
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import snapshot as snap  # noqa: E402


# --------------------------------------------------------------------------
# Fake uiautomation module (faithful to the API surface snapshot.py uses)
# --------------------------------------------------------------------------

# PropertyId / PatternId values mirror the real uiautomation ABI constants so
# the doubles below exercise the same ids the library hands out.
_PATTERN_IDS = {
    "InvokePattern": 10000,
    "ValuePattern": 10002,
    "TogglePattern": 10015,
    "SelectionItemPattern": 10010,
    "ExpandCollapsePattern": 10005,
    "ScrollItemPattern": 10017,
    "SelectionPattern": 10001,
    "TextPattern": 10014,
    "WindowPattern": 10009,
}
_PROP_IDS = {
    "NameProperty": 30005,
    "RuntimeIdProperty": 30000,
    "AutomationIdProperty": 30011,
    "ClassNameProperty": 30012,
    "ControlTypeProperty": 30003,
    "BoundingRectangleProperty": 30001,
    "NativeWindowHandleProperty": 30020,
    "ProcessIdProperty": 30002,
}


class FakePattern:
    def __init__(self, name):
        self.name = name


class FakeControl:
    """Minimal Control stand-in: the attributes snapshot.py actually reads."""

    def __init__(self, name, kind, runtime_id, hwnd=0, pid=0, patterns=(),
                 children=(), rect=None):
        self._name = name
        self._kind = kind
        self._runtime_id = list(runtime_id)
        self._hwnd = hwnd
        self._pid = pid
        self._children = list(children)
        self._patterns = {_PATTERN_IDS[p]: FakePattern(p) for p in patterns}
        self._rect = rect

    # -- properties the real library exposes on Control -------------------
    @property
    def Name(self):
        return self._name

    @property
    def ControlTypeName(self):
        return self._kind

    @property
    def NativeWindowHandle(self):
        return self._hwnd

    @property
    def ProcessId(self):
        return self._pid

    @property
    def AutomationId(self):
        return ""

    @property
    def ClassName(self):
        return ""

    @property
    def BoundingRectangle(self):
        return self._rect

    # -- methods -----------------------------------------------------------
    def GetChildren(self):
        return list(self._children)

    def GetRuntimeId(self):
        if not self._runtime_id:
            raise ValueError("stale element: no runtime id")
        return list(self._runtime_id)

    def GetPattern(self, pattern_id):
        return self._patterns.get(pattern_id)

    def GetTopLevelControl(self):
        return self if self._hwnd else None


def _build_tree():
    """Notepad window (active) with a grandchild; a second window alongside."""
    grandchild = FakeControl("icon", "TextControl", [42, 3], patterns=("TextPattern",))
    save = FakeControl(
        "Save", "ButtonControl", [42, 2], patterns=("InvokePattern",),
        children=[grandchild],
    )
    editor = FakeControl(
        "Text Editor", "EditControl", [42, 1],
        patterns=("ValuePattern", "TextPattern"), rect=(1, 2, 3, 4),
    )
    notepad = FakeControl(
        "Untitled - Notepad", "WindowControl", [42], hwnd=100, pid=4242,
        children=[editor, save],
    )
    seven = FakeControl("7", "ButtonControl", [43, 1], patterns=("InvokePattern",))
    calc = FakeControl(
        "Calculator", "WindowControl", [43], hwnd=200, pid=5150, children=[seven],
    )
    desktop = FakeControl("Desktop", "PaneControl", [], children=[notepad, calc])
    return desktop, notepad, calc


def make_fake_uia(with_cache=True):
    """Build a fake uiautomation module. ``with_cache=False`` mimics the
    released library (2.0.29), where the cached-API surface is commented out."""
    mod = types.SimpleNamespace()
    desktop, notepad, _ = _build_tree()
    mod.PropertyId = types.SimpleNamespace(**_PROP_IDS)
    mod.PatternId = types.SimpleNamespace(**_PATTERN_IDS)
    mod.ControlType = types.SimpleNamespace(WindowControl=50032)
    mod.GetRootControl = lambda: desktop
    mod.GetForegroundControl = lambda: notepad
    if with_cache:
        built = []

        class _Request:
            def __init__(self):
                self.properties = []
                self.patterns = []
                self.TreeScope = None
                self.AutomationElementMode = None
                self.released = False

            def AddProperty(self, prop_id):
                self.properties.append(prop_id)
                return self

            def AddPattern(self, pattern_id):
                self.patterns.append(pattern_id)
                return self

            def Release(self):
                self.released = True

        def build_updated_cache(request):
            built.append(request)
            return notepad  # cached view of the same tree

        notepad.BuildUpdatedCache = build_updated_cache
        mod.CacheRequest = _Request
        mod.TreeScope_Subtree = 5
        mod.AutomationElementMode_Full = 0
        mod._built = built  # test hook
    return mod


@pytest.fixture
def fake_uia(monkeypatch):
    mod = make_fake_uia(with_cache=True)
    monkeypatch.setattr(snap, "_uia", mod)
    return mod


@pytest.fixture
def fake_uia_no_cache(monkeypatch):
    mod = make_fake_uia(with_cache=False)
    monkeypatch.setattr(snap, "_uia", mod)
    return mod


class _JumpClock:
    """Each read after the first burns 10 seconds of budget."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.calls == 1 else self.calls * 10.0


class _JumpClock:
    """Each read after the first burns 10 seconds of budget."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.calls == 1 else self.calls * 10.0


# --------------------------------------------------------------------------
# fast mode
# --------------------------------------------------------------------------

def test_fast_mode_shape(fake_uia):
    s = snap.snapshot(mode="fast")
    assert isinstance(s, snap.Snapshot)

    # windows: every top-level window, with handles/titles/pids
    assert [(w.hwnd, w.title, w.pid) for w in s.windows] == [
        (100, "Untitled - Notepad", 4242),
        (200, "Calculator", 5150),
    ]

    # elements: the active window plus its DIRECT children only
    assert [e.id for e in s.elements] == ["100:42", "100:42,1", "100:42,2"]
    by_id = {e.id: e for e in s.elements}
    assert by_id["100:42"].name == "Untitled - Notepad"
    assert by_id["100:42"].kind == "Window"
    assert by_id["100:42,1"].name == "Text Editor"
    assert by_id["100:42,1"].kind == "Edit"
    assert by_id["100:42,1"].patterns == ["Value", "Text"]
    assert by_id["100:42,1"].rect == [1, 2, 3, 4]
    assert by_id["100:42,2"].patterns == ["Invoke"]

    # the grandchild must not be captured in fast mode
    assert "100:42,3" not in by_id
    assert s.elapsed_ms >= 0


def test_missing_uiautomation_degrades(monkeypatch):
    monkeypatch.setattr(snap, "_uia", None)
    with pytest.raises(NotImplementedError):
        snap.snapshot(mode="fast")


# --------------------------------------------------------------------------
# stable ids
# --------------------------------------------------------------------------

def test_stable_ids_across_calls(fake_uia):
    first = snap.snapshot(mode="full", depth=3)
    second = snap.snapshot(mode="full", depth=3)
    assert [e.id for e in first.elements] == [e.id for e in second.elements]
    for e in first.elements:
        assert re.fullmatch(r"\d+:\d+(,\d+)*", e.id), e.id


def test_id_format_is_hwnd_and_runtime_id(fake_uia):
    s = snap.snapshot(mode="full", depth=3)
    ids = {e.id for e in s.elements}
    assert "100:42,2" in ids          # window 100, runtime id 42,2
    assert "100:42,3" in ids


# --------------------------------------------------------------------------
# full mode
# --------------------------------------------------------------------------

def test_full_mode_uses_cache_request_and_releases(fake_uia):
    s = snap.snapshot(mode="full", depth=3)
    assert len(fake_uia._built) == 1
    request = fake_uia._built[0]

    # the cache request carried props + patterns + scope
    assert snap._NAME_PROP in request.properties
    assert snap._RUNTIME_ID_PROP in request.properties
    assert snap._INVOKE_PATTERN in request.patterns
    assert request.TreeScope is not None

    # try/finally release happened
    assert request.released is True

    # full mode walks past the direct children: grandchild now present
    assert "100:42,3" in {e.id for e in s.elements}


def test_full_mode_depth_limit(fake_uia):
    shallow = snap.snapshot(mode="full", depth=2)
    assert "100:42,3" not in {e.id for e in shallow.elements}
    deep = snap.snapshot(mode="full", depth=3)
    assert "100:42,3" in {e.id for e in deep.elements}


def test_full_mode_live_fallback_without_cache_api(fake_uia_no_cache):
    # uiautomation 2.0.29 ships the cache API commented out: same output shape.
    # full mode walks every enumerated window: notepad's subtree, then calc's.
    s = snap.snapshot(mode="full", depth=3)
    assert [e.id for e in s.elements] == [
        "100:42", "100:42,1", "100:42,2", "100:42,3",
        "200:43", "200:43,1",
    ]
    assert s.skipped == []


def test_type_filter(fake_uia):
    s = snap.snapshot(mode="full", depth=3, type_filter=["Button"])
    assert [(e.name, e.kind) for e in s.elements] == [
        ("Save", "Button"), ("7", "Button"),
    ]


def test_full_mode_root_id(fake_uia):
    s = snap.snapshot(mode="full", root="100:42,2", depth=3)
    # scoped to the Save button subtree
    assert {e.id for e in s.elements} == {"100:42,2", "100:42,3"}


def test_unknown_root_id(fake_uia):
    with pytest.raises(snap.RootNotFound):
        snap.snapshot(mode="full", root="999:1,2", depth=2)


# --------------------------------------------------------------------------
# timeout (per-call budget, skip-not-hang)
# --------------------------------------------------------------------------

def test_timeout_raises_and_releases_cache(fake_uia, monkeypatch):
    monkeypatch.setattr(snap, "_perf_counter", _JumpClock())
    with pytest.raises(snap.SnapshotTimeout):
        snap.snapshot(mode="full", depth=3, timeout_ms=5000)
    # finally released the cache request even on the timeout path
    assert fake_uia._built[0].released is True


def test_timeout_not_triggered_within_budget(fake_uia):
    s = snap.snapshot(mode="full", depth=3, timeout_ms=600_000)
    assert [e.id for e in s.elements] == [
        "100:42", "100:42,1", "100:42,2", "100:42,3",
        "200:43", "200:43,1",
    ]


# --------------------------------------------------------------------------
# per-window isolation: one bad window must never null the snapshot
# --------------------------------------------------------------------------


class _SlowProviderNode:
    """Pathological window: every children hop costs `sleep_s` seconds."""

    def __init__(self, name, pid, handle, sleep_s):
        self._name = name
        self._pid = pid
        self.ControlTypeName = "WindowControl"
        self.NativeWindowHandle = handle
        self._sleep_s = sleep_s

    @property
    def Name(self):
        return self._name

    @property
    def ProcessId(self):
        return self._pid

    @property
    def RuntimeId(self):
        return [self.NativeWindowHandle]

    def GetChildren(self):
        import time as _time

        _time.sleep(self._sleep_s)
        return [_SlowProviderNode("pane", self._pid, self.NativeWindowHandle * 2,
                                  self._sleep_s)]


class _HealthyWindow:
    """Healthy sibling: proves the walk really ran (guards against vacuity)."""

    def __init__(self):
        self.ControlTypeName = "WindowControl"
        self.NativeWindowHandle = 0x1000 + 777

    @property
    def Name(self):
        return "notepad.exe"

    @property
    def ProcessId(self):
        return 777

    @property
    def RuntimeId(self):
        return [self.NativeWindowHandle]

    def GetChildren(self):
        return [_HealthyLeaf()]


class _HealthyLeaf:
    ControlTypeName = "DocumentControl"
    NativeWindowHandle = 0x2000

    @property
    def Name(self):
        return "notes.txt - Notepad"

    @property
    def RuntimeId(self):
        return [0x2000]

    def GetChildren(self):
        return []


@pytest.fixture
def cleared_exclusions():
    snap._EXCLUDED_PROCS.clear()
    yield
    snap._EXCLUDED_PROCS.clear()


def test_slow_window_skipped_healthy_window_kept(monkeypatch, cleared_exclusions):
    """A slow window costs its own sub-budget; the healthy one still lands."""
    slow = _SlowProviderNode("slowui.exe", 4242, 0x900, sleep_s=0.5)
    monkeypatch.setattr(snap, "_list_windows", lambda: [slow, _HealthyWindow()])
    s = snap.snapshot(mode="full", depth=4, timeout_ms=1200)
    blob = str(s).lower()

    # the pathological window was dropped, by pid, and never by name
    assert s.skipped == ["pid:4242"]
    assert "slowui" not in blob

    # the healthy window survived: that is what makes the skip assertion real
    assert any("notes.txt" in e.name for e in s.elements)
    assert [w.title for w in s.windows] == ["notepad.exe"]


def test_excluded_window_is_never_walked(monkeypatch, cleared_exclusions):
    slow = _SlowProviderNode("slowui.exe", 4242, 0x900, sleep_s=5.0)
    monkeypatch.setattr(snap, "_list_windows", lambda: [slow, _HealthyWindow()])
    snap.exclude_proc("slowui.exe")

    started = time.monotonic()
    s = snap.snapshot(mode="full", depth=4, timeout_ms=60_000)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "an excluded provider must cost nothing (skip-not-hang)"
    assert s.skipped == ["pid:4242"]
    assert "slowui" not in str(s).lower()
    assert any("notes.txt" in e.name for e in s.elements)


def test_all_windows_slow_raises_typed_timeout(monkeypatch, cleared_exclusions):
    slow = _SlowProviderNode("slowui.exe", 4242, 0x900, sleep_s=0.5)
    monkeypatch.setattr(snap, "_list_windows", lambda: [slow])
    with pytest.raises(snap.SnapshotTimeout) as excinfo:
        snap.snapshot(mode="full", depth=4, timeout_ms=120)
    assert excinfo.value.code == "SNAPSHOT_TIMEOUT"


# --------------------------------------------------------------------------
# traversal budgets
# --------------------------------------------------------------------------

def test_node_cap_stops_walk(fake_uia, monkeypatch):
    monkeypatch.setattr(snap, "_MAX_NODES", 2)
    s = snap.snapshot(mode="full", depth=3)
    # the cap is per window (isolation), so each window yields 2 elements
    assert len(s.elements) == 4


def test_children_cap_truncates(fake_uia, monkeypatch):
    monkeypatch.setattr(snap, "_MAX_CHILDREN", 1)
    s = snap.snapshot(mode="full", depth=3)
    assert [e.id for e in s.elements] == [
        "100:42", "100:42,1", "200:43", "200:43,1",
    ]


# --------------------------------------------------------------------------
# validation + registry contract
# --------------------------------------------------------------------------

def test_invalid_mode_rejected(fake_uia):
    with pytest.raises(ValueError):
        snap.snapshot(mode="sideways")


def test_depth_bounds_rejected(fake_uia):
    with pytest.raises(ValueError):
        snap.snapshot(mode="full", depth=0)


def test_tools_registry_shape():
    assert len(snap.TOOLS) == 1
    entry = snap.TOOLS[0]
    # dict-style defs are the house contract; both normalizers accept them
    assert entry["name"] == "snapshot"
    assert entry["handler"] is snap.snapshot
    assert entry["description"]
    import pydantic

    assert issubclass(entry["schema"], pydantic.BaseModel)
    schema_props = entry["schema"].model_json_schema()["properties"]
    assert set(schema_props) >= {"mode", "root", "depth", "type_filter"}
