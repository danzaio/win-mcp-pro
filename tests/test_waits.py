"""Tests for tools.waits — single-call polling waits (Task 8).

The provider seam is mocked: waiting handlers resolve the element an id
names (`resolve_element`), read state through element-level calls, and
`wait_focus` compares the focused control's RuntimeId with the target's.
No real UIA tree or Windows session is needed.
"""

import time

import pytest
from pydantic import BaseModel, ValidationError

from win_mcp_pro.tools import waits


# --- fake UIA providers -----------------------------------------------------


class FakeElement:
    """Duck-typed stand-in for a real uiautomation control.

    Mirrors the real API shape: ``Name`` and ``IsEnabled`` are *properties*
    (reading them is how the module observes state) and ``Exists`` is a
    method; the real library has no ``GetName``/``GetEnabled``. The optional
    hooks simulate a value that changes between polls.
    """

    def __init__(self, name="", enabled=True, exists=True, runtime_id=None):
        self._name = name
        self._enabled = enabled
        self._exists = exists
        self.RuntimeId = list(runtime_id) if runtime_id else None
        # functions tests can flip to change behaviour over time
        self.on_get_enabled = None
        self.on_get_name = None
        self.on_exists = None

    @property
    def Name(self):
        if self.on_get_name:
            return self.on_get_name()
        return self._name

    @property
    def IsEnabled(self):
        if self.on_get_enabled:
            return self.on_get_enabled()
        return self._enabled

    def Exists(self, timeout=0):
        if self.on_exists:
            return self.on_exists()
        return self._exists


class FakeProvider:
    """Minimal provider protocol used by waits.

    Resolution and focus are scripted; the element-state reads delegate to
    the real provider so this fake can never drift from the real uiautomation
    API shape again (the live bench caught a method-only read that the old
    fake masked).
    """

    _real = waits.UiaWaitsProvider()

    def __init__(self, elements=None, focus=None):
        self.elements = dict(elements or {})
        self.focus = focus
        self.resolve_calls = 0
        self.focus_calls = 0
        self.enabled_calls = 0
        self.gone_calls = 0
        self.text_calls = 0

    def resolve_element(self, element_id):
        self.resolve_calls += 1
        return self.elements.get(element_id)

    def get_focus(self):
        self.focus_calls += 1
        return self.focus

    def element_text(self, element):
        self.text_calls += 1
        return self._real.element_text(element)

    def element_enabled(self, element):
        self.enabled_calls += 1
        return self._real.element_enabled(element)

    def element_exists(self, element):
        self.gone_calls += 1
        return element.Exists()


ADDR = "1234:5678"


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setattr(waits, "_provider", p, raising=False)
    return p


@pytest.fixture
def no_sleep(monkeypatch):
    """Collapse the polling interval so timeouts stay fast and deterministic."""
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 0)
    monkeypatch.setattr(time, "sleep", lambda _s: None)


# --- tool surface ------------------------------------------------------------


def test_tools_expose_five_wait_tools():
    names = {t.name for t in waits.TOOLS}
    assert names == {"wait", "wait_text", "wait_focus", "wait_enabled", "wait_gone",
                      "wait_change"}
    for t in waits.TOOLS:
        assert t.description
        assert issubclass(t.schema, BaseModel)
        assert callable(t.handler)


def test_schemas_are_pydantic_v2():
    for t in waits.TOOLS:
        schema = t.schema.model_json_schema()
        assert schema.get("type") == "object" or "properties" in schema


# --- wait ---------------------------------------------------------------------


def test_wait_returns_ok_with_elapsed(no_sleep, provider):
    result = waits.handler_wait(ms=120)
    assert result["ok"] is True
    assert result["elapsed_ms"] >= 0


def test_wait_validates_negative_ms(no_sleep, provider):
    with pytest.raises(ValidationError):
        waits.WaitArgs(ms=-1)


def test_wait_zero_ms_allowed(no_sleep, provider):
    result = waits.handler_wait(ms=0)
    assert result["ok"] is True


# --- wait_text ----------------------------------------------------------------


def test_wait_text_immediate_true(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(name="Save completed")
    result = waits.handler_wait_text(s="Save", id=ADDR, timeout_ms=200)
    assert result["ok"] is True
    assert provider.text_calls >= 1


def test_wait_text_timeout_false(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(name="still loading")

    result = waits.handler_wait_text(s="Done", id=ADDR, timeout_ms=0)
    assert result["ok"] is False
    assert result["elapsed_ms"] >= 0


def test_wait_text_empty_string_rejected():
    with pytest.raises(ValidationError):
        waits.WaitTextArgs(s="", id=ADDR)


# --- wait_focus ---------------------------------------------------------------


def test_wait_focus_immediate_true(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(runtime_id=[5678])
    provider.focus = FakeElement(runtime_id=[5678])
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=200)
    assert result["ok"] is True


def test_wait_focus_timeout_false(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(runtime_id=[5678])
    provider.focus = FakeElement(runtime_id=[1])
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_focus_missing_focus_element_is_not_ok(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(runtime_id=[5678])
    provider.focus = None
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_focus_identity_not_readable_never_passes(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(runtime_id=None)
    provider.focus = FakeElement(runtime_id=None)
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_focus_becomes_true_after_polls(no_sleep, provider):
    target = FakeElement(runtime_id=[5678])
    provider.elements[ADDR] = target
    provider.focus = FakeElement(runtime_id=[1])
    calls = {"n": 0}
    real_get_focus = provider.get_focus

    def later():
        calls["n"] += 1
        if calls["n"] > 2:
            provider.focus = target
        return real_get_focus()

    provider.get_focus = later
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=5000)
    assert result["ok"] is True
    assert calls["n"] >= 3


# --- wait_enabled -------------------------------------------------------------


def test_wait_enabled_immediate_true(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(enabled=True)
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=200)
    assert result["ok"] is True
    assert provider.enabled_calls >= 1


def test_wait_enabled_timeout_false(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(enabled=False)
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_enabled_flips_to_true(no_sleep, provider):
    el = FakeElement(enabled=False)
    provider.elements[ADDR] = el
    calls = {"n": 0}

    def later():
        calls["n"] += 1
        return calls["n"] > 1

    el.on_get_enabled = later
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=5000)
    assert result["ok"] is True


def test_wait_enabled_missing_element_is_not_ok(no_sleep, provider):
    provider.elements.clear()
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


# --- wait_gone -----------------------------------------------------------------


def test_wait_gone_immediate_true(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(exists=False)
    result = waits.handler_wait_gone(id=ADDR, timeout_ms=200)
    assert result["ok"] is True


def test_wait_gone_timeout_false(no_sleep, provider):
    provider.elements[ADDR] = FakeElement(exists=True)
    result = waits.handler_wait_gone(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_gone_flips_after_polls(no_sleep, provider):
    el = FakeElement(exists=True)
    provider.elements[ADDR] = el
    calls = {"n": 0}

    def later():
        calls["n"] += 1
        return calls["n"] > 2

    el.on_exists = later
    result = waits.handler_wait_gone(id=ADDR, timeout_ms=5000)
    assert result["ok"] is True


# --- id parsing / safety --------------------------------------------------------


def test_bad_address_rejected():
    with pytest.raises(ValidationError):
        waits.WaitFocusArgs(id="not-an-addr")
    with pytest.raises(ValidationError):
        waits.WaitFocusArgs(id="")


def test_registry_and_snapshot_ids_both_valid():
    # both shapes a caller can hold: a registry digest and "hwnd:runtimeid"
    assert waits.WaitIdArgs(id="0123456789ab").id == "0123456789ab"
    assert waits.WaitIdArgs(id="1234:5678").id == "1234:5678"


def test_provider_errors_do_not_raise_out_of_handler(no_sleep, provider):
    class Boom:
        def GetEnabled(self):
            raise RuntimeError("uia exploded")

        def Exists(self, timeout=0):
            return True

    provider.elements[ADDR] = Boom()
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=0)
    assert result["ok"] is False

    result = waits.handler_wait_gone(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_element_reads_use_the_real_uia_property_shape(no_sleep, provider):
    """Real uiautomation controls expose Name/IsEnabled as properties and
    have no GetName/GetEnabled methods. A control with only that shape must
    still satisfy the waits - the live bench caught the method-only read
    (AttributeError was swallowed into an endless timeout)."""

    class RealShape:
        Name = "Save completed"
        IsEnabled = True
        RuntimeId = [5678]

        def Exists(self, timeout=0):
            return True

    provider.elements[ADDR] = RealShape()
    assert waits.handler_wait_enabled(id=ADDR, timeout_ms=200)["ok"] is True
    assert waits.handler_wait_text(s="save", id=ADDR, timeout_ms=200)["ok"] is True
    assert waits.handler_wait_gone(id=ADDR, timeout_ms=0)["ok"] is False


def test_timeout_bound(no_sleep, provider):
    # huge timeouts must still be clamped sanely (seconds, not years):
    # a never-true condition bails at the clamped deadline, quickly.
    provider.elements[ADDR] = FakeElement(enabled=False)
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=10_000_000_000)
    assert result["ok"] is False
    assert result["elapsed_ms"] <= waits._MAX_TIMEOUT_MS
