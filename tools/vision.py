"""win-mcp-pro - vision fallback: screenshots and region zoom (Task: vision).

Tree-first, image-second: every other module solves its job without pixels.
These two tools exist for the ground the UIA tree cannot cover (Canvas,
WebGL, custom renderers, games): when the tree answers NOT_FOUND, the agent
may look instead of guessing. Microsoft's OmniParser is vision-ONLY, so it
pays image tokens on every step; here the image is the exception, which is
why this module is deliberately small.

Delivery: the PNG is written to a file (the path stays in the text result,
so clients that cannot render images still work) and, when it fits the
inline cap, the same bytes ride back as an MCP image content block under
the reserved ``_image_content`` key. The transport layer splits that key
out of the text part; it never appears in the JSON the model reads.
Over the cap the image is omitted with a reason and only the path remains.
Every capture reports its display origin so pixel results map back to
desktop coordinates.

Import-safe: mss/PIL missing means a typed UNAVAILABLE error at call time,
never at import.
"""
from __future__ import annotations

import base64
import os
import tempfile
import time
from typing import Any, Callable, Mapping

from pydantic import BaseModel, Field

_BACKEND_MISSING = "vision backend unavailable (mss/pillow not installed)"

#: PNGs at or under this size also ride back as MCP image content. Bigger
#: ones stay file-only so a 4K screenshot cannot flood the protocol stream.
_IMAGE_INLINE_MAX_BYTES = 4 * 1024 * 1024


def _shot_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "win-mcp-pro")
    os.makedirs(path, exist_ok=True)
    return path


def _capture(monitor: int = 0):
    """Grab raw pixels; swappable seam for tests.

    Returns ``(rgb_bytes, width, height, origin)`` where ``origin`` is the
    display's desktop offset (``{"left", "top"}``).
    """
    import mss

    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor < 0 or monitor >= len(monitors):
            raise ValueError(f"monitor {monitor} out of range (0..{len(monitors) - 1})")
        area = monitors[monitor] if monitor else sct.monitors[0]
        shot = sct.grab(area)
        origin = {"left": int(area.get("left", 0)), "top": int(area.get("top", 0))}
        return bytes(shot.rgb), int(shot.width), int(shot.height), origin


def _save_png(rgb: bytes, width: int, height: int, tag: str) -> dict:
    from PIL import Image

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(_shot_dir(), f"{tag}-{stamp}.png")
    Image.frombytes("RGB", (width, height), rgb).save(path)
    size = os.path.getsize(path)
    saved = {"path": path, "width": width, "height": height, "bytes": size}
    if size <= _IMAGE_INLINE_MAX_BYTES:
        with open(path, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode("ascii")
        saved["_image_content"] = {"mime": "image/png", "data": payload}
    else:
        saved["image_omitted"] = (
            f"PNG is {size} bytes, over the {_IMAGE_INLINE_MAX_BYTES}-byte inline cap; "
            "read the file at path, or re-capture with a smaller scale"
        )
    return saved


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
        rgb, width, height, origin = _capture(m.monitor)
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
    return {"ok": True, "monitor": m.monitor, "scale": m.scale,
            "origin": origin, **saved}


def handler_zoom(args: Any) -> dict:
    m = ZoomArgs(**dict(args or {}))
    if _dry():
        return {"ok": True, "dry_run": True,
                "region": [m.left, m.top, m.right, m.bottom]}
    try:
        rgb, width, height, origin = _capture(m.monitor)
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
    return {"ok": True, "region": [m.left, m.top, right, bottom],
            "origin": origin, **saved}


def _tool(name: str, description: str, schema, handler: Callable) -> dict:
    return {"name": name, "description": description, "schema": schema, "handler": handler}


TOOLS = [
    _tool("screenshot",
          "Capture a display to a PNG file (vision fallback; tree-first stays "
          "default). The PNG also rides back as an MCP image content block when "
          "it fits the inline cap; over the cap the result keeps the path only.",
          ScreenshotArgs, handler_screenshot),
    _tool("zoom",
          "Crop a display region to a PNG file for reading small text, with the "
          "same inline image delivery as screenshot.",
          ZoomArgs, handler_zoom),
]
