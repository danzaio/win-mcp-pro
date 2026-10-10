"""Tests for tools.waits — single-call polling waits (Task 8).

The uiautomation provider layer is mocked: waits.py talks to a small
provider interface (`_provider`) that the tests replace, so no real
UIA tree or Windows session is needed.
"""

import time

import pytest
from pydantic import BaseModel, ValidationError

from tools import waits


# --- fake UIA providers -----------------------------------------------------


class FakeElement:
    """Duck-typed stand-in for a uiautomation control."""

    def __init__(self, name="", enabled=True, focused=False, exists=True):
        self.Name = name
        self.IsEnabled = enabled
        self._focused = focused
        self._exists = exists
        # functions tests can flip to change behaviour over time
        self.on_get_focus = None
        self.on_get_enabled = None
        self.on_get_name = None
        self.on_exists = None

    def GetFocus(self):
        if self.on_get_focus:
            return self.on_get_focus()
        return self._focused

    def GetEnabled(self):
        if self.on_get_enabled:
            return self.on_get_enabled()
        return self._enabled_value

    @property
    def _enabled_value(self):
        return self.IsEnabled

    def GetName(self):
        if self.on_get_name:
            return self.on_get_name()
        return self.Name

    def Exists(self, timeout=0):
        if self.on_exists:
            return self.on_exists()
        return self._exists


class FakeProvider:
    """Minimal provider protocol used by waits."""

    def __init__(self, root=None, focus=None, text=""):
        self.root = root
        self.focus = focus
        self.text = text
        self.focus_calls = 0
        self.enabled_calls = 0
        self.gone_calls = 0
        self.text_calls = 0

    def get_root(self, hwnd):
        return self.root

    def get_focus(self):
        self.focus_calls += 1
        return self.focus

    def element_text(self, element):
        self.text_calls += 1
        return self.text

    def element_enabled(self, element):
        self.enabled_calls += 1
        return element.GetEnabled()

    def element_exists(self, element):
        self.gone_calls += 1
        return element.Exists()


ADDR = "1234:5678"


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider(root=FakeElement("root"))
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
    assert names == {"wait", "wait_text", "wait_focus", "wait_enabled", "wait_gone"}
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
    provider.text = "Save completed"
    result = waits.handler_wait_text(s="Save", id=ADDR, timeout_ms=200)
    assert result["ok"] is True
    assert provider.text_calls >= 1


def test_wait_text_timeout_false(no_sleep, provider):
    provider.text = "still loading"

    def fake_sleep(_s):
        # advance nothing: real time would also exceed, but with interval 0
        # we rely on deadline logic instead
        return None

    # keep _POLL_INTERVAL_MS at 0 but force real time to pass via monkeypatched clock
    result = waits.handler_wait_text(s="Done", id=ADDR, timeout_ms=0)
    assert result["ok"] is False
    assert result["elapsed_ms"] >= 0


def test_wait_text_empty_string_rejected():
    with pytest.raises(ValidationError):
        waits.WaitTextArgs(s="", id=ADDR)


# --- wait_focus ---------------------------------------------------------------


def test_wait_focus_immediate_true(no_sleep, provider):
    provider.focus = FakeElement(focused=True)
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=200)
    assert result["ok"] is True


def test_wait_focus_timeout_false(no_sleep, provider):
    provider.focus = FakeElement(focused=False)
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_focus_becomes_true_after_polls(no_sleep, provider):
    el = FakeElement(focused=False)
    provider.focus = el
    calls = {"n": 0}

    def later():
        calls["n"] += 1
        return calls["n"] > 2

    el.on_get_focus = later
    result = waits.handler_wait_focus(id=ADDR, timeout_ms=5000)
    assert result["ok"] is True
    assert calls["n"] >= 3


# --- wait_enabled -------------------------------------------------------------


def test_wait_enabled_immediate_true(no_sleep, provider):
    el = FakeElement(enabled=True)
    provider.root = el
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=200)
    assert result["ok"] is True
    assert provider.enabled_calls >= 1


def test_wait_enabled_timeout_false(no_sleep, provider):
    provider.root = FakeElement(enabled=False)
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_enabled_flips_to_true(no_sleep, provider):
    el = FakeElement(enabled=False)
    provider.root = el
    calls = {"n": 0}

    def later():
        calls["n"] += 1
        return calls["n"] > 1

    el.on_get_enabled = later
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=5000)
    assert result["ok"] is True


def test_wait_enabled_missing_element_is_not_ok(no_sleep, provider):
    provider.root = None
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


# --- wait_gone -----------------------------------------------------------------


def test_wait_gone_immediate_true(no_sleep, provider):
    el = FakeElement(exists=False)
    provider.root = el
    result = waits.handler_wait_gone(id=ADDR, timeout_ms=200)
    assert result["ok"] is True


def test_wait_gone_timeout_false(no_sleep, provider):
    provider.root = FakeElement(exists=True)
    result = waits.handler_wait_gone(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_wait_gone_flips_after_polls(no_sleep, provider):
    el = FakeElement(exists=True)
    provider.root = el
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


def test_provider_errors_do_not_raise_out_of_handler(no_sleep, provider):
    class Boom:
        def GetEnabled(self):
            raise RuntimeError("uia exploded")

        def Exists(self, timeout=0):
            return True

    provider.root = Boom()
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=0)
    assert result["ok"] is False

    result = waits.handler_wait_gone(id=ADDR, timeout_ms=0)
    assert result["ok"] is False


def test_timeout_bound(no_sleep, provider):
    # huge timeouts must still be clamped sanely (seconds, not years):
    # a never-true condition bails at the clamped deadline, quickly.
    provider.root = FakeElement(enabled=False)
    result = waits.handler_wait_enabled(id=ADDR, timeout_ms=10_000_000_000)
    assert result["ok"] is False
    assert result["elapsed_ms"] <= waits._MAX_TIMEOUT_MS
