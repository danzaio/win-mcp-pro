"""OCR read fallback (Phase D): read text from pixels via Windows OCR.

For surfaces the accessibility tree cannot describe (canvas, games, images,
custom renderers), ``ocr_read`` captures a monitor region - or the whole
monitor - and recognizes its text with ``Windows.Media.Ocr`` (via the
``winsdk`` projection). The result carries the text, per-line and per-word
bounding boxes, and the capture *frame* id: a word box center can be fed to
``click``/``zoom`` coordinates, so "read the screen, then click the word"
stays frame-pinned.

Import-safe: ``mss``/``PIL``/``winsdk`` missing means a typed UNAVAILABLE at
call time, never at import.
"""
from __future__ import annotations

import asyncio
import io
import os
from typing import Any

from pydantic import BaseModel, Field, model_validator

from tools import vision

_BACKEND_MISSING = "vision backend unavailable (mss/pillow not installed)"
_WINSDK_MISSING = "winsdk is not installed (pip install winsdk)"

#: Payload caps so a wall of text cannot flood the protocol.
MAX_LINES = 400
MAX_WORDS = 2_000


class OcrError(RuntimeError):
    """Typed refusal from the OCR backend."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _dry() -> bool:
    return os.environ.get("DRY_RUN") == "1"


def _grab(monitor: int, region: tuple[int, int, int, int] | None):
    """Capture PNG bytes + size + origin; swappable seam for tests."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor < 0 or monitor >= len(monitors):
            raise ValueError(f"monitor {monitor} out of range (0..{len(monitors) - 1})")
        area = monitors[monitor] if monitor else sct.monitors[0]
        shot = sct.grab(area)
        origin = {"left": int(area.get("left", 0)), "top": int(area.get("top", 0))}
        image = Image.frombytes("RGB", (shot.width, shot.height), bytes(shot.rgb))
        if region is not None:
            left, top, right, bottom = region
            vision._validate_region(left, top, right, bottom, shot.width, shot.height)
            right = min(right, shot.width)
            bottom = min(bottom, shot.height)
            image = image.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), image.width, image.height, origin


def _engine(language: str | None):
    try:
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine
    except Exception as exc:  # noqa: BLE001 - import-safe by contract
        raise OcrError("UNAVAILABLE", "%s: %s" % (_WINSDK_MISSING, exc)) from exc
    if language:
        engine = OcrEngine.try_create_from_language(Language(language))
        if engine is None:
            raise OcrError("NO_OCR_LANGUAGE",
                           "no OCR engine for language %r" % language)
        return engine
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrError("NO_OCR_LANGUAGE",
                       "no OCR language is installed for this user profile")
    return engine


def _run_coroutine(factory):
    """Run an async factory even when called from inside a running loop.

    The MCP server invokes handlers on its asyncio loop, where a plain
    ``asyncio.run`` raises RuntimeError; WinRT is async, so in that case the
    coroutine runs in a private thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


async def _winrt_ocr(png_bytes: bytes, language: str | None):
    """Raw Windows OCR call; swappable seam for tests."""
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = _engine(language)
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png_bytes)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    return await engine.recognize_async(bitmap)


def _recognize(png_bytes: bytes, language: str | None) -> list[dict]:
    """Run Windows OCR over PNG bytes; swappable seam for tests."""
    try:
        result = _run_coroutine(lambda: _winrt_ocr(png_bytes, language))
    except OcrError:
        raise
    except Exception as exc:  # noqa: BLE001 - OCR failure is data
        raise OcrError("OCR_FAILED", str(exc)[:200]) from exc

    lines: list[dict] = []
    total_words = 0
    for line in result.lines:
        words: list[dict] = []
        left = top = right = bottom = None
        for word in line.words:
            rect = word.bounding_rect
            wl, wt = int(rect.x), int(rect.y)
            wr, wb = wl + int(rect.width), wt + int(rect.height)
            words.append({"text": word.text, "bbox": [wl, wt, wr, wb]})
            left = wl if left is None else min(left, wl)
            top = wt if top is None else min(top, wt)
            right = wr if right is None else max(right, wr)
            bottom = wb if bottom is None else max(bottom, wb)
            total_words += 1
            if total_words >= MAX_WORDS:
                break
        if not words:
            continue
        lines.append({"text": line.text, "bbox": [left, top, right, bottom],
                      "words": words})
        if len(lines) >= MAX_LINES or total_words >= MAX_WORDS:
            break
    return lines


# ------------------------------------------------------------------ schema


class OcrReadArgs(BaseModel):
    monitor: int = Field(default=0, ge=0, description="Zero-based display index.")
    left: int | None = Field(default=None, ge=0, description="Region start (all four or none).")
    top: int | None = Field(default=None, ge=0)
    right: int | None = Field(default=None, gt=0)
    bottom: int | None = Field(default=None, gt=0)
    language: str | None = Field(
        default=None,
        description="OCR language tag like 'en-US'; default: the user's installed languages.",
    )

    @model_validator(mode="after")
    def _complete_region(self) -> "OcrReadArgs":
        given = [value is not None for value in (self.left, self.top, self.right, self.bottom)]
        if any(given) and not all(given):
            raise ValueError("give left/top/right/bottom together, or none for the full monitor")
        return self


# ------------------------------------------------------------------ handler


def handler_ocr_read(args: Any) -> dict:
    m = OcrReadArgs(**dict(args or {}))
    if _dry():
        return {"ok": True, "dry_run": True, "monitor": m.monitor}
    region = (m.left, m.top, m.right, m.bottom) if m.left is not None else None
    try:
        png_bytes, width, height, origin = _grab(m.monitor, region)
    except ImportError:
        return {"ok": False, "error": "UNAVAILABLE", "reason": _BACKEND_MISSING}
    except ValueError as exc:
        return {"ok": False, "error": "BAD_REGION", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - capture failure is data
        return {"ok": False, "error": "CAPTURE_FAILED", "reason": str(exc)[:200]}
    try:
        lines = _recognize(png_bytes, m.language)
    except OcrError as exc:
        return {"ok": False, "error": exc.code, "reason": exc.reason}
    frame = vision._frame_register(monitor=m.monitor,
                                   left=origin["left"] + (m.left or 0),
                                   top=origin["top"] + (m.top or 0),
                                   scale=1.0, width=width, height=height)
    text = "\n".join(line["text"] for line in lines)
    return {"ok": True, "text": text, "lines": lines, "frame": frame,
            "width": width, "height": height, "chars": len(text)}


def _tool(name: str, description: str, schema, handler) -> dict:
    return {"name": name, "description": description, "schema": schema,
            "handler": handler}


TOOLS = [
    _tool("ocr_read",
          "Read text from pixels with Windows OCR: captures a monitor (or a "
          "region) and returns the text plus per-line/per-word boxes and the "
          "capture frame id - a word-box center can be clicked through the "
          "frame-pinned path.",
          OcrReadArgs, handler_ocr_read),
]
