"""Tests for the extras pack (goal b76eb4fb).

``type_text`` / ``key_hold`` / stepped drag (tools.compat),
``get_active_window`` / ``open_default`` (tools.winproc), ``win_shot`` /
stepped drag and ``win_shot`` (tools.vision). Seams keep every
test headless: no screen, no SendInput, no launched apps.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from win_mcp_pro.tools import compat, vision, winproc


@pytest.fixture(autouse=True)
def _safe_profile(monkeypatch):
    monkeypatch.setattr(compat, "_unrestricted_flag", False)
    monkeypatch.delenv(compat.UNRESTRICTED_ENV, raising=False)


@pytest.fixture(autouse=True)
def _clean_frames():
    vision._frames.clear()
    yield
    vision._frames.clear()


def _compat_handler(name):
    return next(t["handler"] for t in compat.TOOLS if t["name"] == name)


def _winproc_call(name, args):
    for tool in winproc.TOOLS:
        if tool["name"] == name:
            return tool["handler"](args)
    raise AssertionError(f"tool not registered: {name}")




def test_idle_ms_subtracts_tick_from_last_input(monkeypatch):
    class FakeUser32:
        def GetLastInputInfo(self, info):
            info._obj.dwTime = 9000
            return True

        def GetTickCount(self):
            return 13242

    monkeypatch.setattr(compat, "_user32", lambda: FakeUser32())
    monkeypatch.setattr(compat, "_tick_ms", lambda: 13242)
    assert compat._idle_ms() == 4242


# --- get_active_window ------------------------------------------------------


def test_get_active_window_matches_inventory(monkeypatch):
    monkeypatch.setattr(winproc, "_enum_windows_real",
                        lambda: [(111, "Notes", 222, True), (333, "Other", 444, True)])
    monkeypatch.setattr(winproc, "_foreground_hwnd", lambda: 111)
    result = _winproc_call("get_active_window", {})
    assert result == {"ok": True, "hwnd": 111, "title": "Notes", "pid": 222}


def test_get_active_window_without_foreground_is_typed(monkeypatch):
    monkeypatch.setattr(winproc, "_enum_windows_real", lambda: [])
    monkeypatch.setattr(winproc, "_foreground_hwnd", lambda: None)
    with pytest.raises(winproc.WinProcError) as exc:
        _winproc_call("get_active_window", {})
    assert exc.value.code == "NO_FOREGROUND"


# --- open_default -----------------------------------------------------------


def test_open_default_launches_file(monkeypatch, tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("hi", encoding="utf-8")
    seen = {}

    def fake_shell(t, a, c, s):
        seen["cmd"] = (t, a)
        return 42

    monkeypatch.setattr(winproc, "_shell_execute", fake_shell)
    result = _winproc_call("open_default", {"target": str(target)})
    assert result["ok"] is True and result["target"] == str(target), result
    assert seen["cmd"][0] == str(target)


def test_open_default_accepts_urls_without_files(monkeypatch):
    monkeypatch.setattr(winproc, "_shell_execute", lambda t, a, c, s: 42)
    result = _winproc_call("open_default", {"target": "https://example.com"})
    assert result["ok"] is True, result


def test_open_default_missing_file_is_typed(tmp_path):
    with pytest.raises(winproc.WinProcError) as exc:
        _winproc_call("open_default", {"target": str(tmp_path / "ghost.txt")})
    assert exc.value.code == "FILE_NOT_FOUND"


def test_open_default_shell_failure_is_typed(monkeypatch, tmp_path):
    target = tmp_path / "doc.txt"
    target.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(winproc, "_shell_execute", lambda t, a, c, s: 2)
    with pytest.raises(winproc.WinProcError) as exc:
        _winproc_call("open_default", {"target": str(target)})
    assert exc.value.code == "OPEN_FAILED"


# --- type_text --------------------------------------------------------------


def test_type_text_sends_unicode_pairs_in_one_batch(monkeypatch):
    monkeypatch.setattr(compat, "_bind_window", lambda *a, **k: (None, False))
    sent = []
    monkeypatch.setattr(compat, "_send_input", sent.append)
    result = _compat_handler("type_text")({"text": "aB", "target": "t"})
    assert result == {"ok": True, "chars": 2, "target": "t",
                      "activated": False}, result
    assert len(sent) == 1
    assert [(e["type"], e["scan"], e["up"]) for e in sent[0]] == [
        ("key", ord("a"), False), ("key", ord("a"), True),
        ("key", ord("B"), False), ("key", ord("B"), True)]


def test_type_text_encodes_emoji_as_surrogates(monkeypatch):
    monkeypatch.setattr(compat, "_bind_window", lambda *a, **k: (None, False))
    sent = []
    monkeypatch.setattr(compat, "_send_input", sent.append)
    _compat_handler("type_text")({"text": "\U0001F600", "target": "t"})
    units = [e["scan"] for e in sent[0]]
    assert units == [0xD83D, 0xD83D, 0xDE00, 0xDE00], units


def test_type_text_cap_is_enforced():
    with pytest.raises(ValidationError):
        _compat_handler("type_text")({"text": "x" * 513, "target": "t"})


def test_type_text_without_target_is_refused(monkeypatch):
    monkeypatch.setattr(compat, "_resolve_control", lambda element_id: None)

    def boom(events):
        raise AssertionError("no input may be sent on refusal")

    monkeypatch.setattr(compat, "_send_input", boom)
    result = _compat_handler("type_text")({"text": "hi"})
    assert result["ok"] is False and result["error"] == "TARGET_REQUIRED", result


# --- key_hold ---------------------------------------------------------------


def test_key_hold_down_sleeps_up(monkeypatch):
    monkeypatch.setattr(compat, "_bind_window", lambda *a, **k: (None, False))
    sent = []
    monkeypatch.setattr(compat, "_send_input", sent.append)
    result = _compat_handler("key_hold")({"key": "a", "ms": 0, "target": "t"})
    assert result == {"ok": True, "key": "a", "ms": 0, "target": "t",
                      "activated": False}, result
    assert len(sent) == 2, "down and up go out separately with the hold between"
    assert [e["up"] for batch in sent for e in batch] == [False, True]


def test_key_hold_rejects_chords():
    with pytest.raises(ValidationError):
        _compat_handler("key_hold")({"key": "ctrl+s", "ms": 10, "target": "t"})


# --- stepped drag -----------------------------------------------------------


def test_drag_steps_interpolates_one_batch(monkeypatch):
    calls = {"n": 0}

    def anchor(target_id, frame_id, x, y, *, required):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, (0.0, 0.0)
        return None, (100.0, 200.0)

    monkeypatch.setattr(compat, "_anchor", anchor)
    sent = []
    monkeypatch.setattr(compat, "_send_input", sent.append)
    result = _compat_handler("move")({"x": 0, "y": 0, "from_x": 100, "from_y": 200,
                                      "drag": True, "steps": 4,
                                      "frame": "f", "target": "t"})
    assert result["ok"] is True, result
    assert len(sent) == 1
    moves = [(e["x"], e["y"]) for e in sent[0] if e["action"] == "move"]
    assert moves == [(100.0, 200.0), (75.0, 150.0), (50.0, 100.0),
                     (25.0, 50.0), (0.0, 0.0)], moves


def test_drag_steps_beyond_one_needs_drag():
    with pytest.raises(ValidationError):
        _compat_handler("move")({"x": 1, "y": 2, "steps": 4})


# --- win_shot ---------------------------------------------------------------


def test_win_shot_crops_window_and_registers_frame(monkeypatch):
    monkeypatch.setattr(vision, "_window_rect", lambda hwnd: (50, 60, 150, 110))
    monkeypatch.setattr(vision, "_capture",
                        lambda monitor=0: (b"\x10\x20\x30" * (200 * 120), 200, 120,
                                           {"left": 0, "top": 0}))
    saved = {}
    monkeypatch.setattr(vision, "_save_png", lambda rgb, w, h, tag: {
        "path": "w.png", "width": w, "height": h, "bytes": 9,
        "_saved_size": saved.setdefault("size", (w, h))})
    result = vision.handler_win_shot({"hwnd": 777})
    assert result["ok"] is True, result
    assert saved["size"] == (100, 50), saved
    state, meta = vision.frame_state(result["frame"]["id"])
    assert state == "ok" and (meta["left"], meta["top"]) == (50.0, 60.0)


def test_win_shot_dead_window_is_typed(monkeypatch):
    def boom(hwnd):
        raise ValueError("not a live window")

    monkeypatch.setattr(vision, "_window_rect", boom)
    result = vision.handler_win_shot({"hwnd": 999})
    assert result["ok"] is False and result["error"] == "BAD_WINDOW", result




