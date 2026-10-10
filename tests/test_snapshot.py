"""Tests for tools.snapshot.

Covers: fast-mode shape (active window + direct children only), stable ids,
full-mode subtree walk (CacheRequest when the lib exposes it, live fallback
otherwise), per-call timeout with release, type filter, root-id resolution,
and the missing-library degradation. All UIA access goes through a fake
`uiautomation` module, so nothing here touches the real desktop.
"""
import re
import sys
import types
from pathlib import Path

import pytest

# Repo root on sys.path: tools/ has no __init__.py yet (Task 1 owns it), so
# rely on the namespace-package import plus this explicit path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import snapshot as snap  # noqa: E402


# --------------------------------------------------------------------------
# Fake uiautomation module (faithful to the API surface snapshot.py uses)
# --------------------------------------------------------------------------

# PatternId values: the real ints do not matter, only stable distinct values.
_PATTERN_IDS = {
    "InvokePattern": 10000,
    "ValuePattern": 10002,
    "TogglePattern": 10011,
    "SelectionItemPattern": 10010,
    "ExpandCollapsePattern": 10005,
    "ScrollItemPattern": 10007,
    "SelectionPattern": 10001,
    "TextPattern": 10020,
    "WindowPattern": 10009,
}
_PROP_IDS = {
    "NameProperty": 0,
    "RuntimeIdProperty": 1,
    "AutomationIdProperty": 2,
    "ClassNameProperty": 3,
    "ControlTypeProperty": 4,
    "BoundingRectangleProperty": 5,
    "NativeWindowHandleProperty": 6,
    "ProcessIdProperty": 7,
}


class FakePattern:
    def __init__(self, name):
        self.name = name


class FakeControl:
    """Minimal Control stand-in: the attributes snapshot.py actually reads."""

    def __init__(self, name, kind, runtime_id, hwnd=0, pid=0, patterns=(), children=()):
        self._name = name
        self._kind = kind
        self._runtime_id = list(runtime_id)
        self._hwnd = hwnd
        self._pid = pid
        self._children = list(children)
        self._patterns = {_PATTERN_IDS[p]: FakePattern(p) for p in patterns}

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


class FakeCacheRequest:
    def __init__(self):
        self.properties = []
        self.patterns = []
        self.tree_scope = None
        self.element_mode = None
        self.released = False

    def AddProperty(self, prop_id):
        self.properties.append(prop_id)
        return self

    def AddPattern(self, pattern_id):
        self.patterns.append(pattern_id)
        return self

    def Release(self):
        self.released = True


def _build_tree():
    """Notepad window (active) with a grandchild; a second window alongside."""
    grandchild = FakeControl("icon", "TextControl", [42, 3], patterns=("TextPattern",))
    save = FakeControl("Save", "ButtonControl", [42, 2], patterns=("InvokePattern",), children=[grandchild])
    editor = FakeControl("Text Editor", "EditControl", [42, 1], patterns=("ValuePattern", "TextPattern"))
    notepad = FakeControl("Untitled - Notepad", "WindowControl", [42], hwnd=100, pid=4242, children=[editor, save])
    seven = FakeControl("7", "ButtonControl", [43, 1], patterns=("InvokePattern",))
    calc = FakeControl("Calculator", "WindowControl", [43], hwnd=200, pid=5150, children=[seven])
    desktop = FakeControl("Desktop", "PaneControl", [], children=[notepad, calc])
    return desktop, notepad, calc


def make_fake_uia(with_cache=True):
    """Build a fake uiautomation module. with_cache=False mimics the installed
    2.0.29 build, where the cached-API surface is commented out."""
    mod = types.SimpleNamespace()
    desktop, notepad, _ = _build_tree()
    mod.PropertyId = types.SimpleNamespace(**_PROP_IDS)
    mod.PatternId = types.SimpleNamespace(**_PATTERN_IDS)
    mod.ControlType = types.SimpleNamespace(WindowControl=50032)
    mod.GetRootControl = lambda: desktop
    mod.GetForegroundControl = lambda: notepad
    if with_cache:
        built = []

        class _Request(FakeCacheRequest):
            pass

        def build_updated_cache(control, request):
            built.append(request)
            return control  # cached view of the same tree

        FakeControl.BuildUpdatedCache = build_updated_cache
        mod.CacheRequest = _Request
        mod.TreeScope_Subtree = 5
        mod.AutomationElementMode_Full = 0
        mod._built = built  # test hook
    else:
        # Explicitly no cache surface, like the shipped 2.0.29.
        if hasattr(FakeControl, "BuildUpdatedCache"):
            del FakeControl.BuildUpdatedCache
    return mod


@pytest.fixture
def fake_uia(monkeypatch):
    mod = make_fake_uia(with_cache=True)
    monkeypatch.setattr(snap, "uia", mod)
    return mod


@pytest.fixture
def fake_uia_no_cache(monkeypatch):
    mod = make_fake_uia(with_cache=False)
    monkeypatch.setattr(snap, "uia", mod)
    return mod


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
    assert by_id["100:42"].kind == "WindowControl"
    assert by_id["100:42,1"].name == "Text Editor"
    assert by_id["100:42,1"].kind == "EditControl"
    assert by_id["100:42,1"].patterns == ["Value", "Text"]
    assert by_id["100:42,2"].patterns == ["Invoke"]

    # the grandchild must not be captured in fast mode
    assert "100:42,3" not in by_id
    assert s.elapsed_ms >= 0


def test_missing_uiautomation_degrades(monkeypatch):
    monkeypatch.setattr(snap, "uia", None)
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


def test_id_format_uses_hwnd_and_runtime_id(fake_uia):
    s = snap.snapshot(mode="full", depth=3)
    ids = {e.id for e in s.elements}
    assert "100:42,2" in ids          # window 100, runtime id 42,2
    assert "200:43" in ids


# --------------------------------------------------------------------------
# full mode
# --------------------------------------------------------------------------

def test_full_mode_uses_cache_request_and_releases(fake_uia):
    s = snap.snapshot(mode="full", depth=3)
    assert len(fake_uia._built) == 1
    request = fake_uia._built[0]

    # the cache request carried props + patterns + scope
    assert snap._RUNTIME_ID_PROP in request.properties
    assert snap._NAME_PROP in request.properties
    assert snap._INVOKE_PATTERN in request.patterns
    assert request.tree_scope is not None

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
    s = snap.snapshot(mode="full", depth=3)
    assert [e.id for e in s.elements] == [
        "100:42", "100:42,1", "100:42,2", "100:42,3", "200:43", "200:43,1",
    ]


def test_type_filter(fake_uia):
    s = snap.snapshot(mode="full", depth=3, type_filter=["Button"])
    assert [(e.name, e.kind) for e in s.elements] == [("Save", "ButtonControl"), ("7", "ButtonControl")]


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
    calls = {"n": 0}

    def jump_clock():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else calls["n"] * 10.0  # +10s per read

    monkeypatch.setattr(snap, "_perf_counter", jump_clock)
    with pytest.raises(snap.SnapshotTimeout):
        snap.snapshot(mode="full", depth=3, timeout_ms=5000)
    # finally released the cache request even on the timeout path
    assert fake_uia._built[0].released is True


def test_timeout_not_triggered_within_budget(fake_uia):
    s = snap.snapshot(mode="full", depth=3, timeout_ms=600_000)
    assert len(s.elements) == 6


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
    assert entry.name == "snapshot"
    assert entry.handler is snap.snapshot
    assert issubclass(entry.schema, __import__("pydantic").BaseModel)
    assert entry["name"] == "snapshot"  # mapping access for the registry
    schema_props = entry.schema.model_json_schema()["properties"]
    assert set(schema_props) >= {"mode", "root", "depth", "type_filter"}
