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
import math
import os
import re
import time
from typing import Any, Callable, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

from tools import vision

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
    if not math.isfinite(number):
        raise ValueError("coordinate must be finite")
    if abs(number) > 1_000_000:
        raise ValueError("coordinate is outside any sane desktop range")
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
    target: str | None = Field(
        default=None,
        description="Element id this click intends; required in the safe "
                    "profile so the point is verified against it.",
    )
    frame: str | None = Field(
        default=None,
        description="Frame id from a fresh screenshot/zoom; x/y are image "
                    "pixels then. Required for coordinate clicks in the safe "
                    "profile.",
    )

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
    target: str | None = Field(
        default=None,
        description="Element id this move/drag intends; required for drags in "
                    "the safe profile.",
    )
    frame: str | None = Field(
        default=None,
        description="Frame id from a fresh screenshot/zoom; coordinates are "
                    "image pixels then.",
    )

    _coord = field_validator("x", "y", "from_x", "from_y", mode="before")(_check_coord)

    @model_validator(mode="after")
    def _from_pair(self) -> "MoveArgs":
        if (self.from_x is None) != (self.from_y is None):
            raise ValueError("from_x and from_y go together")
        return self


class ScrollArgs(BaseModel):
    direction: str
    amount: int
    x: float | None = None
    y: float | None = None
    target: str | None = Field(
        default=None,
        description="Element id to scroll at (its center when x/y are given "
                    "as frame pixels).",
    )
    frame: str | None = Field(
        default=None,
        description="Frame id from a fresh screenshot/zoom for x/y.",
    )

    _direction = field_validator("direction", mode="before")(_check_direction)
    _amount = field_validator("amount", mode="before")(_check_amount)
    _coord = field_validator("x", "y", mode="before")(_check_coord)

    @model_validator(mode="after")
    def _both_or_neither(self) -> "ScrollArgs":
        if (self.x is None) != (self.y is None):
            raise ValueError("give both x and y, or neither")
        return self


class ShortcutArgs(BaseModel):
    keys: str
    target: str | None = Field(
        default=None,
        description="Element id whose window must receive the keys; required "
                    "in the safe profile.",
    )
    activate: bool = Field(
        default=True,
        description="Bring the target window to the foreground first when "
                    "needed; refusal when that cannot be confirmed.",
    )

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
#: Absolute coordinates must be expressed over the whole virtual desktop;
#: without this flag SendInput maps 0..65535 onto the primary monitor only,
#: and every coordinate on (or left of) a secondary monitor lands wrong.
MOUSEEVENTF_VIRTUALDESK = 0x4000
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
            record.union.mi.dwFlags = (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                                       | MOUSEEVENTF_VIRTUALDESK)
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


# ------------------------------------------------------------- input binding
#
# Phase B2: every action binds to the element it intends. The default (safe)
# profile refuses coordinate input without the frame id of a fresh capture
# and the target element id; the translated desktop point must land inside
# the target's rectangle and hit-test (and integrity-check) to the target's
# process *before* a single input event is sent. Keyboard input requires the
# target's window to be foreground, with a bounded activation attempt. Every
# refusal returns a typed error with zero SendInput events.
# ``--input-unrestricted`` restores the legacy global input for callers that
# need it; a binding that IS provided is always verified, in either profile.

UNRESTRICTED_ENV = "WINMCP_INPUT_UNRESTRICTED"
_unrestricted_flag = False


def set_unrestricted(enabled: bool) -> None:
    """Switch the input profile (the server entrypoint calls this)."""
    global _unrestricted_flag
    _unrestricted_flag = bool(enabled)


def _unrestricted() -> bool:
    if _unrestricted_flag:
        return True
    return os.environ.get(UNRESTRICTED_ENV, "").strip().lower() in DRY_RUN_VALUES


def _refuse(code: str, reason: str, **extra: Any) -> dict:
    result = {"ok": False, "error": code, "reason": reason}
    result.update(extra)
    return result


def _resolve_control(element_id: str) -> Any:
    """Resolve any published element id to a live control, else None."""
    from tools import patterns

    try:
        return patterns.resolve_element(element_id)
    except Exception:
        return None


def _target_info(control: Any) -> dict | None:
    """Process id + desktop rectangle of a control, or None when unreadable."""
    try:
        rect = control.BoundingRectangle
        left = float(rect.left)
        top = float(rect.top)
        right = float(rect.right)
        bottom = float(rect.bottom)
        pid = int(control.ProcessId)
    except Exception:
        return None
    if right < left or bottom < top or pid <= 0:
        return None
    return {"pid": pid, "rect": (left, top, right, bottom)}


def _check_uipi(pid: int) -> dict:
    from server.security import check_uipi

    return check_uipi(pid)


def _pid_of_window(user32: Any, hwnd: Any) -> int | None:
    if not hwnd:
        return None
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) or None


def _hit_test_pid(x: float, y: float) -> int | None:
    """Pid of the window under (x, y); None when no window is there."""
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    user32 = _user32()
    user32.WindowFromPoint.argtypes = [_POINT]
    user32.WindowFromPoint.restype = ctypes.c_void_p
    hwnd = user32.WindowFromPoint(_POINT(int(round(x)), int(round(y))))
    return _pid_of_window(user32, hwnd)


def _foreground_pid() -> int | None:
    user32 = _user32()
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return _pid_of_window(user32, user32.GetForegroundWindow())


def _activate_window(pid: int) -> bool:
    """Bring a visible top-level window of ``pid`` to the foreground.

    Best effort with a deadline: ``SetForegroundWindow`` can be denied (the
    OS foreground lock), so the caller must treat False as a refusal, never
    as "try anyway".
    """
    from ctypes import wintypes

    user32 = _user32()
    candidates: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

    def _collect(hwnd, _lparam):
        if _pid_of_window(user32, hwnd) == pid and user32.IsWindowVisible(hwnd):
            candidates.append(hwnd)
        return True

    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
    user32.EnumWindows(WNDENUMPROC(_collect), None)
    if not candidates:
        return False
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow(candidates[0])
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline:
        if _foreground_pid() == pid:
            return True
        time.sleep(0.03)
    return _foreground_pid() == pid


def _frame_point(frame_id: str, x: float, y: float):
    """Translate image pixels through a fresh frame, or refuse."""
    state, frame = vision.frame_state(frame_id)
    if state == "unknown":
        return _refuse("FRAME_UNKNOWN",
                       f"no capture frame {frame_id!r} in this session; take a "
                       "screenshot or zoom first"), None
    if state == "stale":
        return _refuse("FRAME_STALE",
                       f"frame {frame_id!r} is older than {vision.FRAME_TTL_MS} ms; "
                       "capture again before clicking"), None
    return None, vision.frame_translate(frame, x, y)


def _verify_target_point(target_id: str, dx: float, dy: float) -> dict | None:
    control = _resolve_control(target_id)
    if control is None:
        return _refuse("TARGET_NOT_FOUND",
                       f"target {target_id!r} does not resolve to a live element")
    info = _target_info(control)
    if info is None:
        return _refuse("TARGET_STALE",
                       f"target {target_id!r} exposes no readable rectangle/process")
    left, top, right, bottom = info["rect"]
    if not (left <= dx <= right and top <= dy <= bottom):
        return _refuse("TARGET_MISMATCH",
                       f"point ({dx:.1f}, {dy:.1f}) is outside target "
                       f"{target_id!r} rect ({left:.0f}, {top:.0f}, "
                       f"{right:.0f}, {bottom:.0f})")
    pid = info["pid"]
    verdict = _check_uipi(pid)
    if not verdict.get("ok"):
        return _refuse(str(verdict.get("code", "INTEGRITY_CHECK_FAILED")),
                       str(verdict.get("detail", "integrity check failed")))
    hit = _hit_test_pid(dx, dy)
    if hit != pid:
        return _refuse("OCCLUDED",
                       f"point ({dx:.1f}, {dy:.1f}) hit-tests to pid {hit!r}, "
                       f"target pid is {pid}; another window covers it")
    return None


def _anchor(target_id: str | None, frame_id: str | None, x: float | None,
            y: float | None, *, required: bool):
    """Resolve the verified desktop point for an action.

    Returns ``(refusal, point)``: on refusal ``point`` is None; otherwise
    ``point`` is None only for a cursor-position action in the unrestricted
    profile. With ``required=False`` (plain hover) an unbound move keeps
    working in the safe profile; anything the caller *provides* is always
    verified.
    """
    safe = not _unrestricted()
    if x is not None:
        if frame_id is not None:
            refusal, point = _frame_point(frame_id, x, y)
            if refusal:
                return refusal, None
            dx, dy = point
        elif safe and (required or target_id is not None):
            return _refuse(
                "FRAME_REQUIRED",
                "coordinate input needs the frame id from a fresh "
                "screenshot/zoom (pass frame=..., or start the server with "
                "--input-unrestricted for raw desktop coordinates)",
            ), None
        else:
            dx, dy = float(x), float(y)
        if target_id is None:
            if safe and required:
                return _refuse(
                    "TARGET_REQUIRED",
                    "input needs the target element id it intends (pass "
                    "target=..., or start the server with "
                    "--input-unrestricted)",
                ), None
            return None, (dx, dy)
        refusal = _verify_target_point(target_id, dx, dy)
        if refusal:
            return refusal, None
        return None, (dx, dy)
    if target_id is not None:
        control = _resolve_control(target_id)
        if control is None:
            return _refuse("TARGET_NOT_FOUND",
                           f"target {target_id!r} does not resolve to a live element"), None
        info = _target_info(control)
        if info is None:
            return _refuse("TARGET_STALE",
                           f"target {target_id!r} exposes no readable rectangle/process"), None
        left, top, right, bottom = info["rect"]
        cx, cy = (left + right) / 2.0, (top + bottom) / 2.0
        refusal = _verify_target_point(target_id, cx, cy)
        if refusal:
            return refusal, None
        return None, (cx, cy)
    if safe and required:
        return _refuse(
            "TARGET_REQUIRED",
            "input needs the target element id it intends (pass target=..., "
            "or start the server with --input-unrestricted)",
        ), None
    return None, None


def _run(name: str, model: type[BaseModel], fn: Callable[[Any], dict], args: Any) -> dict:
    """Coerce args, then honor the DRY_RUN gate before any real work."""
    validated = _coerce(model, args)
    if _dry_run():
        return {"dry_run": True, "action": {"name": name, **validated.model_dump()}}
    return fn(validated)


def _tool_click(args: ClickArgs) -> dict:
    refusal, point = _anchor(args.target, args.frame, args.x, args.y, required=True)
    if refusal:
        return refusal
    events: list[dict] = []
    if point is not None:
        events.append({"type": "mouse", "action": "move", "x": point[0], "y": point[1]})
    events.append({"type": "mouse", "action": "down", "button": args.button})
    events.append({"type": "mouse", "action": "up", "button": args.button})
    _send_input(events)
    result: dict = {"ok": True, "button": args.button}
    if point is not None:
        result["x"], result["y"] = point
    if args.target is not None:
        result["target"] = args.target
    if args.frame is not None:
        result["frame"] = args.frame
    return result


def _tool_move(args: MoveArgs) -> dict:
    refusal, point = _anchor(args.target, args.frame, args.x, args.y,
                             required=args.drag)
    if refusal:
        return refusal
    events: list[dict] = []
    if not args.drag:
        if point is not None:
            events.append({"type": "mouse", "action": "move",
                           "x": point[0], "y": point[1]})
        _send_input(events)
        result: dict = {"ok": True}
        if point is not None:
            result["x"], result["y"] = point
        return result
    # Drag: the press-down point must be verified too, or the button goes
    # down wherever the cursor happens to be (the bug the safe profile
    # exists to prevent).
    if args.from_x is not None:
        start_refusal, start_point = _anchor(args.target, args.frame,
                                             args.from_x, args.from_y, required=True)
        if start_refusal:
            return start_refusal
    elif not _unrestricted():
        return _refuse(
            "FROM_REQUIRED",
            "a drag needs from_x/from_y (image pixels of the same frame) in "
            "the safe profile so the press-down point is verified too",
        )
    else:
        start_point = _cursor_pos()
    assert point is not None and start_point is not None
    events.append({"type": "mouse", "action": "move",
                   "x": start_point[0], "y": start_point[1]})
    events.append({"type": "mouse", "action": "down", "button": "left"})
    events.append({"type": "mouse", "action": "move", "x": point[0], "y": point[1]})
    events.append({"type": "mouse", "action": "up", "button": "left"})
    _send_input(events)
    return {"ok": True}


def _tool_scroll(args: ScrollArgs) -> dict:
    refusal, point = _anchor(args.target, args.frame, args.x, args.y, required=True)
    if refusal:
        return refusal
    events: list[dict] = []
    if point is not None:
        events.append({"type": "mouse", "action": "move", "x": point[0], "y": point[1]})
    delta = args.amount * WHEEL_DELTA
    if args.direction in ("down", "left"):
        delta = -delta
    horizontal = args.direction in ("left", "right")
    events.append({"type": "mouse", "action": "wheel", "delta": delta,
                   "horizontal": horizontal})
    _send_input(events)
    result = {"ok": True, "direction": args.direction, "amount": args.amount}
    if point is not None:
        result["x"], result["y"] = point
    return result


def _tool_shortcut(args: ShortcutArgs) -> dict:
    activated = False
    if args.target is not None:
        control = _resolve_control(args.target)
        if control is None:
            return _refuse("TARGET_NOT_FOUND",
                           f"target {args.target!r} does not resolve to a live element")
        info = _target_info(control)
        if info is None:
            return _refuse("TARGET_STALE",
                           f"target {args.target!r} exposes no readable rectangle/process")
        pid = info["pid"]
        verdict = _check_uipi(pid)
        if not verdict.get("ok"):
            return _refuse(str(verdict.get("code", "INTEGRITY_CHECK_FAILED")),
                           str(verdict.get("detail", "integrity check failed")))
        if _foreground_pid() != pid:
            if not args.activate:
                return _refuse("ACTIVATION_FAILED",
                               f"target pid {pid} is not the foreground window "
                               "and activate=false")
            if not _activate_window(pid):
                return _refuse("ACTIVATION_FAILED",
                               f"could not bring target pid {pid} to the foreground")
            activated = True
    elif not _unrestricted():
        return _refuse(
            "TARGET_REQUIRED",
            "keyboard input needs the target element id whose window should "
            "receive it (pass target=..., or start the server with "
            "--input-unrestricted)",
        )
    tokens = args.keys.split("+")
    codes = [_vk(t) for t in tokens]
    events: list[dict] = []
    for code in codes:
        events.append({"type": "key", "vk": code, "up": False})
    for code in reversed(codes):
        events.append({"type": "key", "vk": code, "up": True})
    _send_input(events)
    result = {"ok": True, "keys": args.keys}
    if args.target is not None:
        result["activated"] = activated
    return result


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
    "Click the left/right/middle mouse button. Safe profile: pass x/y as image "
    "pixels of a fresh screenshot/zoom (frame=...) together with the target "
    "element id; the desktop point is verified against the target before any "
    "input. With target only, clicks the target's center. Either way a target "
    "is required; global cursor/raw-coordinate clicks need "
    "--input-unrestricted."
)
_tool_move.__doc__ = (
    "Move the pointer to (x, y), image pixels of frame=... when given. With "
    "drag=True the button goes down first; the safe profile requires "
    "from_x/from_y plus target/frame so both drag endpoints are verified. "
    "A plain unbound move still works for hovering."
)
_tool_scroll.__doc__ = (
    "Scroll the wheel up/down, or horizontally left/right - at the target "
    "element (its center, or x/y as frame pixels when given). The safe "
    "profile requires target=... so the scroll cannot land on another window."
)
_tool_shortcut.__doc__ = (
    "Type a chord like 'ctrl+s'. The safe profile requires target=...; the "
    "target's window is brought to the foreground first when needed "
    "(activate=false refuses instead). Unbound global keys need "
    "--input-unrestricted."
)
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
