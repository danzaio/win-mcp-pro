"""Tests for tools.ocr - OCR read fallback (Phase D).

The capture (``_grab``) and the recognizer (``_recognize``) are seams, so
these tests run with no screen and no WinRT: they pin the handler contract
(text + line/word boxes + frame registration for frame-pinned clicks) and
the typed refusals.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from tools import ocr, vision


def _png(w=200, h=80) -> bytes:
    image = Image.new("RGB", (w, h), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def fake_grab(monkeypatch):
    def grab(monitor, region):
        return _png(120, 40), 120, 40, {"left": 100, "top": 50}

    monkeypatch.setattr(ocr, "_grab", grab)


@pytest.fixture()
def fake_recognize(monkeypatch):
    def recognize(png_bytes, language):
        return [
            {"text": "HELLO", "bbox": [10, 10, 60, 30],
             "words": [{"text": "HELLO", "bbox": [10, 10, 60, 30]}]},
            {"text": "123", "bbox": [70, 10, 100, 30],
             "words": [{"text": "123", "bbox": [70, 10, 100, 30]}]},
        ]

    monkeypatch.setattr(ocr, "_recognize", recognize)


@pytest.fixture(autouse=True)
def _clean_frames():
    vision._frames.clear()
    yield
    vision._frames.clear()


def test_ocr_read_returns_text_lines_and_clickable_frame(fake_grab, fake_recognize):
    result = ocr.handler_ocr_read({})
    assert result["ok"] is True, result
    assert result["text"] == "HELLO\n123"
    assert result["lines"][0]["bbox"] == [10, 10, 60, 30]
    assert result["lines"][0]["words"][0]["text"] == "HELLO"

    state, meta = vision.frame_state(result["frame"]["id"])
    assert state == "ok"
    assert (meta["left"], meta["top"]) == (100.0, 50.0)
    # a word-box center maps back to desktop coordinates for a frame-pinned click
    x, y = vision.frame_translate(meta, 35, 20)
    assert (x, y) == (135.0, 70.0)


def test_ocr_region_offsets_the_frame(fake_recognize, monkeypatch):
    monkeypatch.setattr(
        ocr, "_grab",
        lambda monitor, region: (_png(50, 20), 50, 20, {"left": 100, "top": 50}),
    )
    result = ocr.handler_ocr_read({"left": 10, "top": 20, "right": 60, "bottom": 40})
    assert result["ok"] is True, result
    assert (result["frame"]["left"], result["frame"]["top"]) == (110.0, 70.0)


def test_ocr_no_language_is_typed(fake_grab, monkeypatch):
    def recognize(png_bytes, language):
        raise ocr.OcrError("NO_OCR_LANGUAGE", "no OCR language is installed")

    monkeypatch.setattr(ocr, "_recognize", recognize)
    result = ocr.handler_ocr_read({})
    assert result["ok"] is False and result["error"] == "NO_OCR_LANGUAGE", result


def test_ocr_missing_backend_is_typed(fake_grab, monkeypatch):
    def recognize(png_bytes, language):
        raise ocr.OcrError("UNAVAILABLE", "winsdk is not installed")

    monkeypatch.setattr(ocr, "_recognize", recognize)
    result = ocr.handler_ocr_read({})
    assert result["error"] == "UNAVAILABLE", result


def test_ocr_bad_region_is_typed(monkeypatch):
    def grab(monitor, region):
        raise ValueError("region needs left<right and top<bottom")

    monkeypatch.setattr(ocr, "_grab", grab)
    result = ocr.handler_ocr_read({"left": 5, "top": 5, "right": 5, "bottom": 6})
    assert result["error"] == "BAD_REGION", result


def test_ocr_region_must_be_complete():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ocr.OcrReadArgs(left=1, top=2)


def test_ocr_dry_run_never_touches_the_screen(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    called = []
    monkeypatch.setattr(
        ocr, "_grab",
        lambda monitor, region: called.append(1) or (_png(4, 4), 4, 4, {"left": 0, "top": 0}),
    )
    result = ocr.handler_ocr_read({})
    assert result.get("dry_run") is True
    assert called == []


def test_tool_surface():
    names = {t["name"] for t in ocr.TOOLS}
    assert names == {"ocr_read"}
    for tool in ocr.TOOLS:
        assert tool["description"].strip()
        assert callable(tool["handler"])
        assert hasattr(tool["schema"], "model_json_schema")
