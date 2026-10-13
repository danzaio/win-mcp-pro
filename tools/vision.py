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

Frames: every capture also registers a short-lived *frame* (id + desktop
position + scale) in this process. Coordinate input (tools.compat) quotes
that frame id, so a click can only use pixels from a fresh capture and the
translated desktop point is verified against the target before anything is
sent. ``FRAME_TTL_MS`` and ``FRAME_CAP`` bound freshness and memory.

Import-safe: mss/PIL missing means a typed UNAVAILABLE error at call time,
never at import.
"""
from __future__ import annotations

import base64
import ctypes
import os
import tempfile
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

_BACKEND_MISSING = "vision backend unavailable (mss/pillow not installed)"

#: PNGs at or under this size also ride back as MCP image content. Bigger
#: ones stay file-only so a 4K screenshot cannot flood the protocol stream.
_IMAGE_INLINE_MAX_BYTES = 4 * 1024 * 1024

#: Coordinate frames expire quickly: a click should follow a fresh look.
FRAME_TTL_MS = 30_000

#: Frames kept in memory; the oldest ones are evicted first.
FRAME_CAP = 16

_frames: "OrderedDict[str, dict]" = OrderedDict()


def _now_ms() -> float:
    """Monotonic clock seam (tests can freeze it)."""
    return time.monotonic() * 1000.0


def _frame_register(*, monitor: int, left: float, top: float, scale: float,
                    width: int, height: int) -> dict:
    """Record where an image sits on the desktop; return its frame metadata."""
    frame = {
        "id": uuid.uuid4().hex[:12],
        "monitor": int(monitor),
        "left": float(left),
        "top": float(top),
        "scale": float(scale) if scale else 1.0,
        "width": int(width),
        "height": int(height),
        "captured_at_ms": _now_ms(),
    }
    _frames[frame["id"]] = frame
    while len(_frames) > FRAME_CAP:
        _frames.popitem(last=False)
    return dict(frame)


def frame_state(frame_id: str) -> tuple[str, dict | None]:
    """``("ok"|"stale"|"unknown", frame)`` for a frame id.

    ``stale`` still returns the metadata so callers can explain what aged
    out; ``unknown`` means this process never minted that id.
    """
    frame = _frames.get(frame_id)
    if frame is None:
        return "unknown", None
    if _now_ms() - frame["captured_at_ms"] > FRAME_TTL_MS:
        return "stale", dict(frame)
    return "ok", dict(frame)


def frame_translate(frame: Mapping[str, Any], x: float, y: float) -> tuple[float, float]:
    """Image pixel (x, y) of ``frame`` -> desktop coordinate."""
    scale = frame.get("scale") or 1.0
    return (float(frame["left"]) + x / scale, float(frame["top"]) + y / scale)


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


class DisplaysArgs(BaseModel):
    pass


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
    frame = _frame_register(monitor=m.monitor, left=origin["left"],
                            top=origin["top"], scale=m.scale,
                            width=width, height=height)
    return {"ok": True, "monitor": m.monitor, "scale": m.scale,
            "origin": origin, "frame": frame, **saved}


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
    frame = _frame_register(monitor=m.monitor,
                            left=origin["left"] + m.left,
                            top=origin["top"] + m.top,
                            scale=1.0,
                            width=saved["width"], height=saved["height"])
    return {"ok": True, "region": [m.left, m.top, right, bottom],
            "origin": origin, "frame": frame, **saved}


def _tool(name: str, description: str, schema, handler: Callable) -> dict:
    return {"name": name, "description": description, "schema": schema, "handler": handler}


def _enum_displays() -> list[dict]:
    """One entry per monitor: name, desktop rect, primary flag, DPI.

    ``EnumDisplayMonitors`` + ``GetMonitorInfoW`` have described every
    Windows display since XP; ``GetDpiForMonitor`` (8.1+) is best-effort and
    comes back null where the OS will not say. Swappable seam for tests.
    """
    user32 = ctypes.windll.user32
    shcore = getattr(ctypes.windll, "shcore", None)

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _INFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong),
                    ("szDevice", ctypes.c_wchar * 32)]

    found: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.POINTER(_RECT), ctypes.c_void_p)
    def _callback(hmonitor, _hdc, _rect, _data):
        info = _INFO()
        info.cbSize = ctypes.sizeof(_INFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            rc = info.rcMonitor
            dpi: list[int] | None = None
            if shcore is not None:
                dx, dy = ctypes.c_uint(0), ctypes.c_uint(0)
                try:
                    if shcore.GetDpiForMonitor(int(hmonitor), 0,
                                              ctypes.byref(dx), ctypes.byref(dy)) == 0:
                        dpi = [int(dx.value), int(dy.value)]
                except Exception:  # noqa: BLE001 - DPI is best-effort
                    dpi = None
            found.append({
                "index": len(found),
                "name": info.szDevice,
                "left": int(rc.left), "top": int(rc.top),
                "width": int(rc.right - rc.left),
                "height": int(rc.bottom - rc.top),
                "primary": bool(info.dwFlags & 1),
                "dpi": dpi,
            })
        return 1

    if not user32.EnumDisplayMonitors(None, None, _callback, 0):
        raise OSError("EnumDisplayMonitors failed")
    return found


def handler_displays(args: Any) -> dict:
    """List the attached displays with geometry, primary flag and DPI."""
    try:
        displays = _enum_displays()
    except Exception as exc:  # noqa: BLE001 - enumeration failure is data
        return {"ok": False, "error": "DISPLAY_UNREADABLE", "reason": str(exc)[:200]}
    return {"ok": True, "count": len(displays), "displays": displays}


_IMG_FORMATS = {
    "png": "PNG", "jpeg": "JPEG", "jpg": "JPEG",
    "pdf": "PDF", "bmp": "BMP", "gif": "GIF",
}


class ImgConvertArgs(BaseModel):
    path: str = Field(description="source image file (must exist)")
    format: str | None = Field(
        default=None,
        description="target format: png, jpeg, pdf, bmp or gif "
                    "(default: from the output extension, else png)",
    )
    output: str | None = Field(
        default=None,
        description="destination file; default: alongside the source with the new extension",
    )
    max_width: int | None = Field(
        default=None, gt=0,
        description="downscale so the width fits (aspect kept); omit to keep pixels",
    )

    @field_validator("format", mode="before")
    @classmethod
    def _known_format(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str) or value.strip().lower() not in _IMG_FORMATS:
            raise ValueError(f"format must be one of {', '.join(sorted(_IMG_FORMATS))}")
        return value.strip().lower()


def handler_img_convert(args: Any) -> dict:
    """Re-encode (and optionally downscale) an image file.

    The receipt-to-PDF shaped task with no screen involved: read pixels from
    disk, write them back in another format. Never overwrites its own input.
    """
    import os.path

    try:
        from PIL import Image
    except ImportError:
        return {"ok": False, "error": "UNAVAILABLE", "reason": _BACKEND_MISSING}
    m = ImgConvertArgs(**dict(args or {}))
    if not os.path.isfile(m.path):
        return {"ok": False, "error": "FILE_NOT_FOUND",
                "reason": f"no file at {m.path!r}"}
    fmt = m.format
    if fmt is None and m.output is not None:
        fmt = _IMG_FORMATS.get(os.path.splitext(m.output)[1].lstrip(".").lower())
    fmt = fmt or "png"
    if fmt not in _IMG_FORMATS:
        return {"ok": False, "error": "BAD_FORMAT",
                "reason": f"cannot infer a format from {m.output!r}"}
    ext = ".jpg" if fmt in ("jpeg", "jpg") else f".{fmt}"
    output = m.output or f"{os.path.splitext(m.path)[0]}{ext}"
    if os.path.abspath(output) == os.path.abspath(m.path):
        return {"ok": False, "error": "INPUT_PROTECTED",
                "reason": "output would overwrite the input file"}
    try:
        image = Image.open(m.path)
        image.load()
        if fmt in ("jpeg", "jpg") or fmt == "pdf":
            image = image.convert("RGB")
        if m.max_width is not None and image.width > m.max_width:
            image = image.resize(
                (m.max_width, max(1, round(image.height * m.max_width / image.width))),
                Image.LANCZOS)
        image.save(output, _IMG_FORMATS[fmt])
    except Exception as exc:  # noqa: BLE001 - corrupt input is data
        return {"ok": False, "error": "CONVERT_FAILED", "reason": str(exc)[:200]}
    return {"ok": True, "output": output, "format": fmt,
            "width": image.width, "height": image.height,
            "bytes": os.path.getsize(output)}


TOOLS = [
    _tool("screenshot",
          "Capture a display to a PNG file (vision fallback; tree-first stays "
          "default). The PNG also rides back as an MCP image content block when "
          "it fits the inline cap; over the cap the result keeps the path only. "
          "The result carries a short-lived frame id that click/drag/scroll "
          "accept as frame=..., pinning their coordinates to this capture.",
          ScreenshotArgs, handler_screenshot),
    _tool("zoom",
          "Crop a display region to a PNG file for reading small text, with the "
          "same inline image delivery and frame id as screenshot; click regions "
          "measured from the crop use that frame.",
          ZoomArgs, handler_zoom),
    _tool("displays",
          "List the attached displays: name, desktop rect, primary flag and "
          "DPI (best-effort). Read-only geometry for grounding follow-ups.",
          DisplaysArgs, handler_displays),
    _tool("img_convert",
          "Re-encode an image file (png/jpeg/pdf/bmp/gif) with optional "
          "downscale. File-to-file pixels with no screen involved; never "
          "overwrites its input.",
          ImgConvertArgs, handler_img_convert),
]
