"""Opt-in live OCR test (Phase D).

Run explicitly::

    WINMCP_RUN_LIVE_OCR=1 python -m pytest tests/test_live_ocr.py -q

Runs the real ``Windows.Media.Ocr`` recognizer over a synthetic image
(never a desktop capture) and through the handler with a faked grab, so
the whole result contract is exercised against the real engine.
"""

from __future__ import annotations

import io
import os
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

from tools import ocr, vision

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("WINMCP_RUN_LIVE_OCR") != "1",
    reason="opt-in live OCR smoke; requires Windows OCR language packs",
)


def _text_png() -> bytes:
    image = Image.new("RGB", (460, 140), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 50), "HELLO 123 WINDOWS", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_live_ocr_reads_synthetic_text():
    lines = ocr._recognize(_text_png(), None)
    text = " ".join(line["text"] for line in lines).upper()
    assert "HELLO" in text, text
    assert "123" in text, text
    assert lines[0]["words"], "word boxes must come back for clicking"


def test_live_ocr_handler_with_real_recognizer(monkeypatch):
    png = _text_png()
    monkeypatch.setattr(
        ocr, "_grab",
        lambda monitor, region: (png, 460, 140, {"left": 0, "top": 0}),
    )
    vision._frames.clear()
    result = ocr.handler_ocr_read({})
    assert result["ok"] is True, result
    assert "HELLO" in result["text"].upper(), result["text"]
    state, meta = vision.frame_state(result["frame"]["id"])
    assert state == "ok"
    assert meta["width"] == 460 and meta["height"] == 140
