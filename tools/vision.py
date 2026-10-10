"""win-mcp-pro - vision fallback: screenshots and region zoom (Task: vision).

Tree-first, image-second: every other module solves its job without pixels.
These two tools exist for the ground the UIA tree cannot cover (Canvas,
WebGL, custom renderers, games): when the tree answers NOT_FOUND, the agent
may look instead of guessing. Microsoft's OmniParser is vision-ONLY, so it
pays image tokens on every step; here the image is the exception, which is
why this module is deliberately small.

Images are files, not context: handlers return a PNG path under the temp
dir plus dimensions, never base64 blobs. Import-safe: mss/PIL missing means
a typed UNAVAILABLE error at call time, never at import.
"""
from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Callable, Mapping

from pydantic import BaseModel, Field

_BACKEND_MISSING = "vision backend unavailable (mss/pillow not installed)"


def _shot_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "win-mcp-pro")
    os.makedirs(path, exist_ok=True)
    return path


def _capture(monitor: int = 0):
    """Grab raw pixels; swappable seam for tests. Returns (rgb_bytes, w, h)."""
    import mss

    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor < 0 or monitor >= len(monitors):
            raise ValueError(f"monitor {monitor} out of range (0..{len(monitors) - 1})")
        shot = sct.grab(monitors[monitor] if monitor else sct.monitors[0])
        return bytes(shot.rgb), int(shot.width), int(shot.height)


def _save_png(rgb: bytes, width: int, height: int, tag: str) -> dict:
    from PIL import Image

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(_shot_dir(), f"{tag}-{stamp}.png")
    Image.frombytes("RGB", (width, height), rgb).save(path)
    return {"path": path, "width": width, "height": height,
            "bytes": os.path.getsize(path)}


def _dry() -> bool:
    return os.environ.get("DRY_RUN") == "1"


class ScreenshotArgs(BaseModel):
    monitor: int = Field(default=0, ge=0, description="Zero-based display index.")
    scale: float = Field(default=1.0, gt=0.0, le=1.0,
                         description="Downscale factor; 0.5 halves each side.")


class ZoomArgs(BaseModel):
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    right: int = Field(gt=0)
    bottom: int = Field(gt=0)
    monitor: int = Field(default=0, ge=0)


def _validate_region(left: int, top: int, right: int, bottom: int,
                     width: int, height: int) -> None:
    if right <= left or bottom <= top:
        raise ValueError("region needs left<right and top<bottom")
    if left >= width or top >= height:
        raise ValueError("region starts outside the display")


def handler_screenshot(args: Any) -> dict:
    m = ScreenshotArgs(**dict(args or {}))
    if _dry():
        return {"ok": True, "dry_run": True, "monitor": m.monitor, "scale": m.scale}
    try:
        rgb, width, height = _capture(m.monitor)
    except ImportError:
        return {"ok": False, "error": "UNAVAILABLE", "reason": _BACKEND_MISSING}
    except Exception as exc:  # noqa: BLE001 - capture failure is data
        return {"ok": False, "error": "CAPTURE_FAILED", "reason": str(exc)[:200]}
    if m.scale < 1.0:
        from PIL import Image

        full = Image.frombytes("RGB", (width, height), rgb)
        small = full.resize((max(1, int(width * m.scale)), max(1, int(height * m.scale))))
        width, height = small.size
        rgb = small.tobytes()
    saved = _save_png(rgb, width, height, "shot")
    return {"ok": True, "monitor": m.monitor, "scale": m.scale, **saved}


def handler_zoom(args: Any) -> dict:
    m = ZoomArgs(**dict(args or {}))
    if _dry():
        return {"ok": True, "dry_run": True,
                "region": [m.left, m.top, m.right, m.bottom]}
    try:
        rgb, width, height = _capture(m.monitor)
    except ImportError:
        return {"ok": False, "error": "UNAVAILABLE", "reason": _BACKEND_MISSING}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "CAPTURE_FAILED", "reason": str(exc)[:200]}
    try:
        _validate_region(m.left, m.top, m.right, m.bottom, width, height)
    except ValueError as exc:
        return {"ok": False, "error": "BAD_REGION", "reason": str(exc)}
    right, bottom = min(m.right, width), min(m.bottom, height)
    from PIL import Image

    crop = Image.frombytes("RGB", (width, height), rgb).crop((m.left, m.top, right, bottom))
    saved = _save_png(crop.tobytes(), *crop.size, "zoom")
    return {"ok": True, "region": [m.left, m.top, right, bottom], **saved}


def _tool(name: str, description: str, schema, handler: Callable) -> dict:
    return {"name": name, "description": description, "schema": schema, "handler": handler}


TOOLS = [
    _tool("screenshot",
          "Capture a display to a PNG file (vision fallback; tree-first stays default).",
          ScreenshotArgs, handler_screenshot),
    _tool("zoom",
          "Crop a display region to a PNG file for reading small text.",
          ZoomArgs, handler_zoom),
]
