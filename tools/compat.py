"""Compat tools: coordinate-level pointer, keyboard and clipboard input.

These are the escape hatch for apps without a usable UIA tree. The spec
prefers Control Patterns (see tools/patterns.py and tools/values.py), but
Canvas, games and custom-rendered surfaces leave no choice, so there has
to be a coordinate path — deliberately slower, deliberately logged.

Every tool here goes through one gate first: when DRY_RUN is on nothing
reaches the machine, and the caller gets the action back instead. Real
input funnels through ``_send_input`` / ``_clipboard_read`` /
``_clipboard_write`` so tests never touch the desktop.
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from typing import Any, Callable, Mapping

from pydantic import BaseModel, field_validator, model_validator

BUTTONS = ("left", "right", "middle")
DIRECTIONS = ("up", "down", "left", "right")
DRY_RUN_VALUES = {"1", "true", "yes", "on"}

KEYS_RE = re.compile(r"^[a-z0-9]{1,15}(\+[a-z0-9]{1,15})*$")

CLIPBOARD_TEXT_MAX = 100_000

# Virtual-key codes for the tokens a shortcut string may use.
VK: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "option": 0x12,
    "win": 0x5B,
    "cmd": 0x5B,
    "super": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "bksp": 0x08,
    "del": 0x2E,
    "delete": 0x2E,
    "ins": 0x2D,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pgup": 0x21,
    "pageup": 0x21,
    "pgdn": 0x22,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "capslock": 0x14,
    "prtsc": 0x2C,
    "printscreen": 0x2C,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x70 + _i - 1


# ------------------------------------------------------------------ schemas


def _check_coord(value: Any) -> float:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("coordinate must be a number, not a bool")
    number = float(value)
    if number < 0:
        raise ValueError("coordinate must be >= 0")
    return number


def _check_button(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("button must be a string")
    button = value.strip().lower()
    if button not in BUTTONS:
        raise ValueError(f"button must be one of {', '.join(BUTTONS)}")
    return button


def _check_direction(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("direction must be a string")
    direction = value.strip().lower()
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
    return direction


def _check_amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("amount must be an integer number of ticks")
    if value < 1:
        raise ValueError("amount must be >= 1")
    return value


def _check_keys(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("keys must be a string like 'ctrl+s'")
    keys = value.strip().lower()
    if not KEYS_RE.match(keys):
        raise ValueError("keys must be '+' joined tokens (a-z, 0-9), e.g. ctrl+shift+p")
    return keys


def _check_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    if len(value) > CLIPBOARD_TEXT_MAX:
        raise ValueError(f"text exceeds the {CLIPBOARD_TEXT_MAX} char clipboard budget")
    return value


class ClickArgs(BaseModel):
    x: float | None = None
    y: float | None = None
    button: str = "left"

    _coord = field_validator("x", "y", mode="before")(_check_coord)
    _button = field_validator("button", mode="before")(_check_button)

    @model_validator(mode="after")
    def _both_or_neither(self) -> "ClickArgs":
        if (self.x is None) != (self.y is None):
            raise ValueError("give both x and y, or neither to click the cursor")
        return self


class MoveArgs(BaseModel):
    x: float
    y: float
    drag: bool = False
    from_x: float | None = None
    from_y: float | None = None

    _coord = field_validator("x", "y", "from_x", "from_y", mode="before")(_check_coord)

    @model_validator(mode="after")
    def _from_pair(self) -> "MoveArgs":
        if (self.from_x is None) != (self.from_y is None):
            raise ValueError("from_x and from_y go together")
        return self


class ScrollArgs(BaseModel):
    direction: str
    amount: int

    _direction = field_validator("direction", mode="before")(_check_direction)
    _amount = field_validator("amount", mode="before")(_check_amount)


class ShortcutArgs(BaseModel):
    keys: str

    _keys = field_validator("keys", mode="before")(_check_keys)


class ClipboardGetArgs(BaseModel):
    pass


class ClipboardSetArgs(BaseModel):
    text: str

    _text = field_validator("text", mode="before")(_check_text)


# ------------------------------------------------------------------ win32

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000

KEYEVENTF_KEYUP = 0x0002

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

WHEEL_DELTA = 120

BUTTON_FLAGS = {
    ("down", "left"): MOUSEEVENTF_LEFTDOWN,
    ("up", "left"): MOUSEEVENTF_LEFTUP,
    ("down", "right"): MOUSEEVENTF_RIGHTDOWN,
    ("up", "right"): MOUSEEVENTF_RIGHTUP,
    ("down", "middle"): MOUSEEVENTF_MIDDLEDOWN,
    ("up", "middle"): MOUSEEVENTF_MIDDLEUP,
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_ulong),
        ("dy", ctypes.c_ulong),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


def _user32():
    dll = ctypes.windll.user32
    dll.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    dll.SendInput.restype = ctypes.c_uint
    return dll


def _abs_xy(x: float, y: float) -> tuple[int, int]:
    """Map desktop pixels to the 0..65535 absolute space SendInput wants."""
    user32 = _user32()
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    dx = (x - left) * 65535 / (width - 1) if width > 1 else 0
    dy = (y - top) * 65535 / (height - 1) if height > 1 else 0
    return int(max(0, dx)), int(max(0, dy))


def _to_input(event: dict) -> INPUT:
    record = INPUT()
    if event["type"] == "mouse":
        action = event["action"]
        record.type = INPUT_MOUSE
        if action == "move":
            dx, dy = _abs_xy(event["x"], event["y"])
            record.union.mi.dx = dx
            record.union.mi.dy = dy
            record.union.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
        elif action in ("down", "up"):
            record.union.mi.dwFlags = BUTTON_FLAGS[(action, event["button"])]
        elif action == "wheel":
            record.union.mi.dwFlags = (
                MOUSEEVENTF_HWHEEL if event.get("horizontal") else MOUSEEVENTF_WHEEL
            )
            record.union.mi.mouseData = event["delta"] & 0xFFFFFFFF
        else:  # pragma: no cover - guarded by callers
            raise ValueError(f"unknown mouse action {action!r}")
        return record
    if event["type"] == "key":
        record.type = INPUT_KEYBOARD
        record.union.ki.wVk = event["vk"]
        record.union.ki.dwFlags = KEYEVENTF_KEYUP if event["up"] else 0
        return record
    raise ValueError(f"unknown input type {event['type']!r}")  # pragma: no cover


def _send_input(events: list[dict]) -> None:
    """Push raw input. Swapped out in tests; never called when DRY_RUN=1."""
    records = (INPUT * len(events))(*[_to_input(e) for e in events])
    sent = _user32().SendInput(len(events), records, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(f"SendInput accepted {sent} of {len(events)} events")


def _cursor_pos() -> tuple[int, int]:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    if not _user32().GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed")
    return point.x, point.y


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _open_clipboard(user32) -> None:
    # Another window can hold the clipboard for a moment; retry briefly
    # instead of failing the whole tool call.
    for attempt in range(5):
        if user32.OpenClipboard(None):
            return
        if attempt == 4:
            raise OSError("could not open the clipboard (another window holds it?)")
        time.sleep(0.01)


def _clipboard_read() -> str:
    """Read text from the clipboard. Raises when there is no text."""
    user32 = _user32()
    kernel32 = ctypes.windll.kernel32
    _open_clipboard(user32)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            raise RuntimeError("no text on the clipboard")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("GlobalLock failed")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_write(text: str) -> None:
    user32 = _user32()
    kernel32 = ctypes.windll.kernel32
    payload = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise OSError("GlobalAlloc failed")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        raise OSError("GlobalLock failed")
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    _open_clipboard(user32)
    try:
        if not user32.EmptyClipboard():
            raise OSError("EmptyClipboard failed")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ handlers


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in DRY_RUN_VALUES


def _coerce(model: type[BaseModel], args: Any) -> BaseModel:
    if isinstance(args, model):
        return args
    if isinstance(args, Mapping):
        return model.model_validate(args)
    raise TypeError(f"expected {model.__name__} or a mapping, got {type(args).__name__}")


def _vk(token: str) -> int:
    if token in VK:
        return VK[token]
    if len(token) == 1 and token.isalnum():
        return ord(token.upper())
    raise ValueError(f"unknown key {token!r} in shortcut")


def _run(name: str, model: type[BaseModel], fn: Callable[[Any], dict], args: Any) -> dict:
    """Coerce args, then honor the DRY_RUN gate before any real work."""
    validated = _coerce(model, args)
    if _dry_run():
        return {"dry_run": True, "action": {"name": name, **validated.model_dump()}}
    return fn(validated)


def _tool_click(args: ClickArgs) -> dict:
    events: list[dict] = []
    if args.x is not None:
        events.append({"type": "mouse", "action": "move", "x": args.x, "y": args.y})
    events.append({"type": "mouse", "action": "down", "button": args.button})
    events.append({"type": "mouse", "action": "up", "button": args.button})
    _send_input(events)
    return {"ok": True, "x": args.x, "y": args.y, "button": args.button}


def _tool_move(args: MoveArgs) -> dict:
    events: list[dict] = []
    if not args.drag:
        events.append({"type": "mouse", "action": "move", "x": args.x, "y": args.y})
    else:
        if args.from_x is not None:
            start = (args.from_x, args.from_y)
        else:
            start = _cursor_pos()
        events.append({"type": "mouse", "action": "move", "x": start[0], "y": start[1]})
        events.append({"type": "mouse", "action": "down", "button": "left"})
        events.append({"type": "mouse", "action": "move", "x": args.x, "y": args.y})
        events.append({"type": "mouse", "action": "up", "button": "left"})
    _send_input(events)
    return {"ok": True}


def _tool_scroll(args: ScrollArgs) -> dict:
    delta = args.amount * WHEEL_DELTA
    if args.direction in ("down", "left"):
        delta = -delta
    horizontal = args.direction in ("left", "right")
    _send_input(
        [{"type": "mouse", "action": "wheel", "delta": delta, "horizontal": horizontal}]
    )
    return {"ok": True, "direction": args.direction, "amount": args.amount}


def _tool_shortcut(args: ShortcutArgs) -> dict:
    tokens = args.keys.split("+")
    codes = [_vk(t) for t in tokens]
    events: list[dict] = []
    for code in codes:
        events.append({"type": "key", "vk": code, "up": False})
    for code in reversed(codes):
        events.append({"type": "key", "vk": code, "up": True})
    _send_input(events)
    return {"ok": True, "keys": args.keys}


def _tool_clipboard_get(args: ClipboardGetArgs) -> dict:
    return {"ok": True, "text": _clipboard_read()}


def _tool_clipboard_set(args: ClipboardSetArgs) -> dict:
    _clipboard_write(args.text)
    return {"ok": True, "chars": len(args.text)}


def _bind(name: str, model: type[BaseModel], fn: Callable[[Any], dict]) -> dict:
    return {
        "name": name,
        "description": str(fn.__doc__ or "").strip(),
        "schema": model,
        "handler": lambda args, _n=name, _m=model, _f=fn: _run(_n, _m, _f, args),
    }


_tool_click.__doc__ = (
    "Click the left/right/middle mouse button. Without x/y clicks where the "
    "cursor is; with both it moves there first."
)
_tool_move.__doc__ = (
    "Move the pointer to (x, y). With drag=True the button goes down first "
    "(at from_x/from_y or the cursor) and up at (x, y)."
)
_tool_scroll.__doc__ = "Scroll the wheel up/down, or horizontally left/right."
_tool_shortcut.__doc__ = "Type a chord like 'ctrl+s' or 'ctrl+shift+p'."
_tool_clipboard_get.__doc__ = "Read text from the Windows clipboard."
_tool_clipboard_set.__doc__ = "Put text on the Windows clipboard."

TOOLS: list[dict] = [
    _bind("click", ClickArgs, _tool_click),
    _bind("move", MoveArgs, _tool_move),
    _bind("scroll", ScrollArgs, _tool_scroll),
    _bind("shortcut", ShortcutArgs, _tool_shortcut),
    _bind("clipboard_get", ClipboardGetArgs, _tool_clipboard_get),
    _bind("clipboard_set", ClipboardSetArgs, _tool_clipboard_set),
]
