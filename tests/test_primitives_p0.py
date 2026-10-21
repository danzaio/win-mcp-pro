"""Tests for the P0 input/observation primitives (post-E).

``cursor_position`` / ``double_click`` (tools.compat), ``ocr_click``
(tools.ocr) and ``wait_change`` (tools.waits). Every seam is swappable so
these run headless: no screen, no SendInput, no WinRT.
"""

from __future__ import annotations

import pytest

from win_mcp_pro.tools import compat, ocr, vision, waits


@pytest.fixture(autouse=True)
def _clean_frames():
    vision._frames.clear()
    yield
    vision._frames.clear()


@pytest.fixture(autouse=True)
def _safe_profile(monkeypatch):
    monkeypatch.setattr(compat, "_unrestricted_flag", False)
    monkeypatch.delenv(compat.UNRESTRICTED_ENV, raising=False)


def _handler(name):
    return next(t["handler"] for t in compat.TOOLS if t["name"] == name)


# --- cursor_position --------------------------------------------------------


def test_cursor_position_reads_the_desktop_point(monkeypatch):
    monkeypatch.setattr(compat, "_cursor_pos", lambda: (300, 400))
    result = _handler("cursor_position")({})
    assert result == {"ok": True, "x": 300, "y": 400}


def test_cursor_position_failure_is_typed(monkeypatch):
    def boom():
        raise OSError("GetCursorPos failed")

    monkeypatch.setattr(compat, "_cursor_pos", boom)
    result = _handler("cursor_position")({})
    assert result["ok"] is False and result["error"] == "CURSOR_UNREADABLE", result


# --- double_click -----------------------------------------------------------


def test_double_click_sends_one_batch_down_up_down_up(monkeypatch):
    monkeypatch.setattr(compat, "_anchor", lambda *a, **k: (None, (100.0, 200.0)))
    sent = []
    monkeypatch.setattr(compat, "_send_input", sent.append)
    result = _handler("double_click")({"x": 10, "y": 10,
                                       "frame": "f", "target": "t"})
    assert result["ok"] is True, result
    assert len(sent) == 1, "the pair must go out as a single SendInput batch"
    actions = [(e["action"], e.get("button")) for e in sent[0]]
    assert actions == [("move", None), ("down", "left"), ("up", "left"),
                       ("down", "left"), ("up", "left")], actions


def test_double_click_without_binding_is_refused(monkeypatch):
    def boom(events):
        raise AssertionError("no input may be sent on refusal")

    monkeypatch.setattr(compat, "_send_input", boom)
    result = _handler("double_click")({"x": 10, "y": 10})
    assert result["ok"] is False, result
    assert result["error"] in ("FRAME_REQUIRED", "TARGET_REQUIRED"), result


def test_double_click_unknown_target_sends_nothing(monkeypatch):
    monkeypatch.setattr(compat, "_resolve_control", lambda element_id: None)
    monkeypatch.setattr(compat, "_frame_point",
                        lambda frame_id, x, y: (None, (10.0, 10.0)))
    def boom(events):
        raise AssertionError("no input may be sent on refusal")

    monkeypatch.setattr(compat, "_send_input", boom)
    result = _handler("double_click")({"target": "nope"})
    assert result["error"] == "TARGET_NOT_FOUND", result


# --- ocr_click --------------------------------------------------------------


def _fake_lines():
    return [
        {"text": "Commit Alt", "bbox": [10, 10, 110, 30],
         "words": [{"text": "Commit", "bbox": [10, 10, 60, 30]},
                   {"text": "Alt", "bbox": [70, 10, 100, 30]}]},
    ]


@pytest.fixture()
def fake_ocr(monkeypatch):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (120, 40), "white").save(buffer, format="PNG")
    png = buffer.getvalue()
    monkeypatch.setattr(
        ocr, "_grab",
        lambda monitor, region: (png, 120, 40, {"left": 100, "top": 50}),
    )
    monkeypatch.setattr(ocr, "_recognize", lambda png_bytes, language: _fake_lines())


def test_ocr_click_clicks_the_matching_word_center(monkeypatch, fake_ocr):
    monkeypatch.setattr(compat, "_verify_target_point", lambda *a: None)
    sent = []
    monkeypatch.setattr(compat, "_send_input", sent.append)
    result = ocr.handler_ocr_click({"text": "alt", "target": "w"})
    assert result["ok"] is True, result
    assert result["text"] == "Alt"
    # word box [70,10,100,30] centers on (85, 20) image px -> +(100, 50) origin
    assert (result["x"], result["y"]) == (185.0, 70.0)
    assert len(sent) == 1
    assert [e["action"] for e in sent[0]] == ["move", "down", "up"]
    state, _ = vision.frame_state(result["frame"]["id"])
    assert state == "ok"


def test_ocr_click_missing_word_is_typed_and_clicks_nothing(monkeypatch, fake_ocr):
    def boom(events):
        raise AssertionError("no input may be sent on refusal")

    monkeypatch.setattr(compat, "_send_input", boom)
    result = ocr.handler_ocr_click({"text": "zzz", "target": "w"})
    assert result["ok"] is False and result["error"] == "TEXT_NOT_FOUND", result


def test_ocr_click_without_target_refuses_before_capture(monkeypatch):
    grabbed = []
    monkeypatch.setattr(ocr, "_grab", lambda monitor, region: grabbed.append(1))
    result = ocr.handler_ocr_click({"text": "Alt"})
    assert result["error"] == "TARGET_REQUIRED", result
    assert grabbed == [], "refusal must come before the expensive capture"


# --- wait_change ------------------------------------------------------------


def _rgb(w, h, white_block=None):
    data = bytearray(w * h * 3)
    if white_block:
        x0, y0, x1, y1 = white_block
        for y in range(y0, y1):
            for x in range(x0, x1):
                base = (y * w + x) * 3
                data[base] = data[base + 1] = data[base + 2] = 255
    return bytes(data)


@pytest.fixture()
def scripted_capture(monkeypatch):
    frames = {"shots": []}

    def capture(monitor=0):
        return frames["shots"].pop(0), 100, 100, {"left": 0, "top": 0}

    monkeypatch.setattr(vision, "_capture", capture)
    return frames


def test_wait_change_fires_on_pixel_change(scripted_capture, monkeypatch):
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 5)
    same = _rgb(100, 100)
    changed = _rgb(100, 100, (10, 10, 30, 30))
    scripted_capture["shots"] = [same, same, changed]
    result = waits.handler_wait_change({"timeout_ms": 2000})
    assert result["ok"] is True, result
    assert result["changed"] > 0.02, result


def test_wait_change_times_out_when_static(scripted_capture, monkeypatch):
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 5)
    scripted_capture["shots"] = [_rgb(100, 100)] * 500
    result = waits.handler_wait_change({"timeout_ms": 120})
    assert result["ok"] is False, result
    assert result["changed"] == 0.0, result


def test_wait_change_threshold_gates_small_diffs(scripted_capture, monkeypatch):
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 5)
    # 20x20 white on 100x100 = 4% of pixels: fires at 0.02, not at 1.0
    changed = _rgb(100, 100, (10, 10, 30, 30))
    scripted_capture["shots"] = [_rgb(100, 100), changed]
    assert waits.handler_wait_change(
        {"timeout_ms": 1000, "threshold": 0.02})["ok"] is True
    scripted_capture["shots"] = [_rgb(100, 100), changed]
    assert waits.handler_wait_change(
        {"timeout_ms": 300, "threshold": 1.0})["ok"] is False


def test_wait_change_bad_region_is_typed(scripted_capture):
    scripted_capture["shots"] = [_rgb(100, 100)]
    result = waits.handler_wait_change(
        {"left": 90, "top": 90, "right": 80, "bottom": 95, "timeout_ms": 100})
    assert result["ok"] is False and result["error"] == "BAD_REGION", result
