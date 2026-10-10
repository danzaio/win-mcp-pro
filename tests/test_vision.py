"""Tests for tools.vision — screenshot/zoom fallback (mocked backend, no screen)."""
import os

import pytest
from pydantic import BaseModel, ValidationError

from tools import vision


def _fake_capture(rgb_byte=b"\x7f", w=64, h=48):
    def _cap(monitor=0):
        assert monitor == 0
        return rgb_byte * (w * h * 3), w, h
    return _cap


@pytest.fixture()
def fake_shot(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "_capture", _fake_capture())
    monkeypatch.setattr(vision, "_shot_dir", lambda: str(tmp_path))
    monkeypatch.delenv("DRY_RUN", raising=False)


def test_tools_contract():
    assert {t["name"] for t in vision.TOOLS} == {"screenshot", "zoom"}
    for t in vision.TOOLS:
        assert t["description"]
        assert issubclass(t["schema"], BaseModel)
        assert callable(t["handler"])


def test_screenshot_writes_png(fake_shot):
    res = vision.handler_screenshot({})
    assert res["ok"] is True
    assert res["width"] == 64 and res["height"] == 48
    assert res["path"].endswith(".png") and os.path.isfile(res["path"])
    assert res["bytes"] > 0


def test_screenshot_scale_halves(fake_shot):
    res = vision.handler_screenshot({"scale": 0.5})
    assert res["ok"] is True
    assert (res["width"], res["height"]) == (32, 24)


def test_screenshot_rejects_bad_scale():
    with pytest.raises(ValidationError):
        vision.ScreenshotArgs(scale=0)
    with pytest.raises(ValidationError):
        vision.ScreenshotArgs(scale=2.0)


def test_screenshot_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    res = vision.handler_screenshot({})
    assert res == {"ok": True, "dry_run": True, "monitor": 0, "scale": 1.0}


def test_zoom_crops_region(fake_shot):
    res = vision.handler_zoom({"left": 8, "top": 8, "right": 24, "bottom": 24})
    assert res["ok"] is True
    assert res["region"] == [8, 8, 24, 24]
    assert os.path.isfile(res["path"])


def test_zoom_rejects_degenerate_region(fake_shot):
    res = vision.handler_zoom({"left": 10, "top": 10, "right": 10, "bottom": 20})
    assert res["ok"] is False and res["error"] == "BAD_REGION"


def test_zoom_clamps_to_display(fake_shot):
    res = vision.handler_zoom({"left": 0, "top": 0, "right": 9999, "bottom": 9999})
    assert res["ok"] is True
    assert res["region"] == [0, 0, 64, 48]


def test_zoom_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    res = vision.handler_zoom({"left": 1, "top": 1, "right": 2, "bottom": 2})
    assert res["dry_run"] is True


def test_missing_backend_is_typed_error(monkeypatch, tmp_path):
    def _boom(monitor=0):
        raise ImportError("no mss")
    monkeypatch.setattr(vision, "_capture", _boom)
    res = vision.handler_screenshot({})
    assert res["ok"] is False and res["error"] == "UNAVAILABLE"
