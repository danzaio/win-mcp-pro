"""Tests for tools.vision — screenshot/zoom fallback (mocked backend, no screen)."""
import asyncio
import base64
import json
import os

import pytest
from pydantic import BaseModel, ValidationError

from server import main, registry
from tools import vision


def _fake_capture(rgb_byte=b"\x7f", w=64, h=48, origin=(0, 0)):
    def _cap(monitor=0):
        assert monitor == 0
        return rgb_byte * (w * h * 3), w, h, {"left": origin[0], "top": origin[1]}
    return _cap


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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


# --- inline image envelope ----------------------------------------------------


def test_screenshot_includes_inline_image(fake_shot):
    res = vision.handler_screenshot({})
    assert res["ok"] is True
    payload = res["_image_content"]
    assert payload["mime"] == "image/png"
    raw = base64.b64decode(payload["data"])
    assert raw.startswith(PNG_MAGIC)
    assert os.path.isfile(res["path"])


def test_zoom_includes_inline_image(fake_shot):
    res = vision.handler_zoom({"left": 8, "top": 8, "right": 24, "bottom": 24})
    assert res["ok"] is True
    raw = base64.b64decode(res["_image_content"]["data"])
    assert raw.startswith(PNG_MAGIC)


def test_screenshot_reports_display_origin(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "_capture", _fake_capture(origin=(-1920, 0)))
    monkeypatch.setattr(vision, "_shot_dir", lambda: str(tmp_path))
    res = vision.handler_screenshot({})
    assert res["origin"] == {"left": -1920, "top": 0}


def test_over_cap_omits_image_but_keeps_path(fake_shot, monkeypatch):
    monkeypatch.setattr(vision, "_IMAGE_INLINE_MAX_BYTES", 10)
    res = vision.handler_screenshot({})
    assert res["ok"] is True
    assert "_image_content" not in res
    assert "cap" in res["image_omitted"]
    assert os.path.isfile(res["path"])


def test_dry_run_keeps_no_image(fake_shot, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    res = vision.handler_screenshot({})
    assert "_image_content" not in res


# --- MCP route: text part + image part ----------------------------------------


def _drive_vision_route():
    registry.reset_registry()
    registry.register_module(vision.TOOLS)
    server = main.build_server()

    async def run():
        from mcp import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams

        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(
                server.run(*server_streams, server.create_initialization_options(),
                           raise_exceptions=True)
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    return await session.call_tool("screenshot", {})
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_mcp_route_returns_text_and_image(fake_shot):
    result = _drive_vision_route()
    assert result.is_error is False
    kinds = [c.type for c in result.content]
    assert kinds == ["text", "image"], kinds
    text = json.loads(result.content[0].text)
    assert text["ok"] is True
    assert "_image_content" not in text  # the transport key never lands in the text part
    assert os.path.isfile(text["path"])
    image = result.content[1]
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data).startswith(PNG_MAGIC)


def test_mcp_route_over_cap_returns_text_only(fake_shot, monkeypatch):
    monkeypatch.setattr(vision, "_IMAGE_INLINE_MAX_BYTES", 10)
    result = _drive_vision_route()
    kinds = [c.type for c in result.content]
    assert kinds == ["text"], kinds
    text = json.loads(result.content[0].text)
    assert "cap" in text["image_omitted"]
