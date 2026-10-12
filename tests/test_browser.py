"""Tests for tools.browser - isolated Chromium DOM backend over CDP (Phase C).

The CDP transport is a single seam (``browser._call``) and browser launch is
another (``browser._ensure_launched``), so every test here runs with a fake
protocol: no browser process, no network, no desktop. The live opt-in test
lives in tests/test_live_browser.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tools import browser


class FakeCdp:
    """Records calls; answers from a scripted response map."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def __call__(self, method, params=None, *, session=False, timeout=None):
        self.calls.append({"method": method, "params": params, "session": session})
        if method in self.responses:
            value = self.responses[method]
            return value() if callable(value) else value
        raise AssertionError("unexpected CDP call: %s" % method)

    def methods(self):
        return [c["method"] for c in self.calls]


class Sequenced:
    """Callable response returning queued values in order."""

    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


@pytest.fixture()
def browser_state(monkeypatch):
    browser._reset()
    state = browser._STATE
    state.update({"session": "sess-1", "target": "target-1", "port": 9999,
                  "profile": None, "proc": None, "headless": True})
    monkeypatch.setattr(browser, "_ensure_launched", lambda headless=True: state)
    yield state
    browser._reset()


# ------------------------------------------------------------------ launch


def test_launch_args_use_an_isolated_profile():
    args = browser._launch_args("C:/edge/msedge.exe", "C:/tmp/prof", True)
    assert "--user-data-dir=C:/tmp/prof" in args
    assert "--remote-debugging-port=0" in args
    assert "--headless=new" in args
    assert "--no-first-run" in args and "--no-default-browser-check" in args
    headed = browser._launch_args("C:/edge/msedge.exe", "C:/tmp/prof", False)
    assert not any(a.startswith("--headless") for a in headed)


def test_find_browser_prefers_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "browser.exe"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setenv("WINMCP_BROWSER_PATH", str(fake))
    assert browser._find_browser() == str(fake)

    monkeypatch.delenv("WINMCP_BROWSER_PATH")
    monkeypatch.setattr(browser, "_BROWSER_CANDIDATES", ())
    assert browser._find_browser() is None


def test_no_browser_is_a_typed_refusal(monkeypatch):
    def boom(headless=True):
        raise browser.BrowserError("NO_BROWSER", "no Edge/Chrome found")

    monkeypatch.setattr(browser, "_ensure_launched", boom)
    result = browser.handler_open({})
    assert result["ok"] is False and result["error"] == "NO_BROWSER"


# ------------------------------------------------------------------ open


def test_open_creates_target_attaches_and_reports_page(browser_state, monkeypatch):
    fake = FakeCdp({
        "Target.createTarget": {"targetId": "t-9"},
        "Target.attachToTarget": {"sessionId": "s-9"},
        "Page.enable": {},
        "Runtime.evaluate": Sequenced([
            {"result": {"value": "complete"}},
            {"result": {"value": ["data:text/html,hi", "Hi"]}},
        ]),
    })
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_open({"url": "data:text/html,hi"})
    assert result["ok"] is True, result
    assert result["page"]["url"] == "data:text/html,hi"
    assert result["page"]["title"] == "Hi"
    assert browser_state["session"] == "s-9"
    create = next(c for c in fake.calls if c["method"] == "Target.createTarget")
    assert create["params"] == {"url": "data:text/html,hi"}
    attach = next(c for c in fake.calls if c["method"] == "Target.attachToTarget")
    assert attach["params"]["flatten"] is True


# ------------------------------------------------------------- snapshot


def test_snapshot_builds_refs_from_the_ax_tree(browser_state, monkeypatch):
    fake = FakeCdp({
        "Runtime.evaluate": {"result": {"value": ["data:x", "T"]}},
        "Accessibility.getFullAXTree": {"nodes": [
            {"role": {"value": "button"}, "name": {"value": "Go"},
             "backendDOMNodeId": 11},
            {"role": {"value": "textbox"}, "name": {"value": "Name"},
             "backendDOMNodeId": 12},
            {"role": {"value": "generic"}, "name": {"value": ""},
             "backendDOMNodeId": 13},
            {"role": {"value": "button"}, "name": {"value": "Hidden"},
             "backendDOMNodeId": 14, "ignored": True},
        ]},
    })
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_snapshot({})
    assert result["ok"] is True, result
    names = {r["name"] for r in result["refs"]}
    assert names == {"Go", "Name"}, result
    assert result["count"] == 2
    ref = next(r["ref"] for r in result["refs"] if r["name"] == "Go")
    assert browser._REFS[ref]["backend"] == 11


# ------------------------------------------------------------- actions


def test_click_dispatches_at_the_box_center(browser_state, monkeypatch):
    browser._REFS["b1"] = {"backend": 11, "role": "button", "name": "Go"}
    fake = FakeCdp({
        "DOM.getBoxModel": {"model": {"content": [10, 20, 110, 20, 110, 60, 10, 60]}},
        "Input.dispatchMouseEvent": {},
    })
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_click({"ref": "b1"})
    assert result["ok"] is True, result
    assert result["at"] == [60.0, 40.0]
    dispatches = [c for c in fake.calls if c["method"] == "Input.dispatchMouseEvent"]
    assert [d["params"]["type"] for d in dispatches] == ["mousePressed", "mouseReleased"]
    assert dispatches[0]["params"]["x"] == 60.0
    assert dispatches[0]["params"]["y"] == 40.0
    assert dispatches[0]["params"]["button"] == "left"
    assert all(d["session"] for d in dispatches)


def test_click_unknown_ref_refuses_without_touching_the_browser(browser_state, monkeypatch):
    fake = FakeCdp({})
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_click({"ref": "nope"})
    assert result["error"] == "UNKNOWN_REF", result
    assert fake.calls == []


def test_click_gone_node_is_a_stale_ref_refusal(browser_state, monkeypatch):
    browser._REFS["b1"] = {"backend": 11, "role": "button", "name": "Go"}

    def gone():
        raise browser.BrowserError("CDP_ERROR", "node does not exist")

    fake = FakeCdp({"DOM.getBoxModel": gone})
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_click({"ref": "b1"})
    assert result["error"] == "STALE_REF", result
    assert not any(c["method"] == "Input.dispatchMouseEvent" for c in fake.calls)


def test_fill_focuses_then_inserts_text(browser_state, monkeypatch):
    browser._REFS["b1"] = {"backend": 11, "role": "textbox", "name": "Name"}
    fake = FakeCdp({"DOM.focus": {}, "Input.insertText": {}})
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_fill({"ref": "b1", "text": "hello"})
    assert result["ok"] is True, result
    assert fake.methods() == ["DOM.focus", "Input.insertText"]
    assert fake.calls[1]["params"] == {"text": "hello"}


def test_read_body_text_without_ref(browser_state, monkeypatch):
    fake = FakeCdp({"Runtime.evaluate": {"result": {"value": "before after"}}})
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_read({})
    assert result["ok"] is True and result["text"] == "before after"


def test_read_resolves_ref_to_its_text(browser_state, monkeypatch):
    browser._REFS["b1"] = {"backend": 11, "role": "div", "name": "out"}
    fake = FakeCdp({
        "DOM.resolveNode": {"object": {"objectId": "obj-1"}},
        "Runtime.callFunctionOn": {"result": {"value": "after"}},
    })
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_read({"ref": "b1"})
    assert result["ok"] is True and result["text"] == "after"
    call = next(c for c in fake.calls if c["method"] == "Runtime.callFunctionOn")
    assert call["params"]["objectId"] == "obj-1"
    assert call["params"]["returnByValue"] is True


# ------------------------------------------------------------- lifecycle


class FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakeWs:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_close_kills_owned_process_and_removes_profile(browser_state, tmp_path, monkeypatch):
    proc = FakeProc()
    ws = FakeWs()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "file").write_text("x", encoding="utf-8")
    browser._STATE.update({"proc": proc, "ws": ws, "profile": str(profile)})
    monkeypatch.setattr(browser, "_call", FakeCdp({}))
    result = browser.handler_close({})
    assert result["ok"] is True and result["closed"] is True
    assert proc.terminated and ws.closed
    assert not profile.exists()
    assert browser._STATE == {}


def test_status_reports_absence_without_launching(browser_state, monkeypatch):
    browser._reset()
    fake = FakeCdp({})
    monkeypatch.setattr(browser, "_call", fake)
    result = browser.handler_status({})
    assert result["ok"] is True and result["running"] is False
    assert fake.calls == []


# ------------------------------------------------------------- schemas


def test_tool_surface_and_schemas():
    names = {t["name"] for t in browser.TOOLS}
    assert names == {"browser_open", "browser_status", "browser_snapshot",
                     "browser_click", "browser_fill", "browser_read",
                     "browser_close"}
    for tool in browser.TOOLS:
        assert tool["description"].strip()
        assert callable(tool["handler"])
        assert hasattr(tool["schema"], "model_json_schema")


def test_click_schema_requires_ref():
    with pytest.raises(ValidationError):
        browser.ClickArgs()
