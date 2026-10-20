"""win-mcp-pro - window and process tools, RE flavoured (Task 9).

Pure ``ctypes`` against user32/kernel32/advapi32/ntdll/shell32: no pywin32, so
the module imports on any machine and only raises when a call really needs
Windows.

What it gives the agent that a plain "click" tool never does: who owns a
window, what the process command line really was, whether the target is
elevated (UIPI will silently swallow your keystrokes otherwise), and where to
draw a window on the right monitor.

Deliberately no screenshot and no input synthesis here: this module only
*moves windows* and *kills processes*, and killing is gated behind
``confirm=True`` plus a hard "never myself, never pid 0/4" rule.

Typed failures raise :class:`WinProcError`, which carries ``.code`` so the
server can answer with a typed error instead of a hang. Handlers are
single-positional callables compatible with the ``tools`` registry contract
(``handler(args_mapping)``).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "TOOLS",
    "WinProcError",
    "exclude_proc",
    "is_excluded",
    "filter_excluded",
    "clear_exclusions",
    "excluded_names",
    "normalize_exe",
]

_IS_WINDOWS = sys.platform.startswith("win")

# ---------------------------------------------------------------------------
# typed errors
# ---------------------------------------------------------------------------


class WinProcError(RuntimeError):
    """Deliberate failure with a machine-readable ``code``."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": str(self), **self.detail}


def _win32_message() -> str:
    """Text of the last win32 error (functions are bound with use_last_error)."""
    code = ctypes.get_last_error()
    if not code:
        return ""
    try:
        return f"{ctypes.WinError(code).strerror} (win32 {code})"
    except Exception:  # pragma: no cover - WinError is safe, belt and braces
        return f"win32 {code}"


# ---------------------------------------------------------------------------
# ctypes bindings
# ---------------------------------------------------------------------------

_MAX_PATH = 260

# Window commands / positioning.
_SW_HIDE, _SW_SHOWNORMAL, _SW_MINIMIZE, _SW_MAXIMIZE, _SW_RESTORE = 0, 1, 6, 3, 9
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010
_HWND_TOP = 0
_WM_CLOSE = 0x0010
_STARTF_USESHOWWINDOW = 0x00000001
_CREATE_UNICODE_ENVIRONMENT = 0x00000400

# Access rights.
_TH32CS_SNAPPROCESS = 0x00000002

#: Pid 4 is the System process and 0 is the idle process: never terminate.


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wt.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * _MAX_PATH),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_user32 = _kernel32 = _shell32 = None


def _bind(dll, name: str, restype, *argtypes) -> None:
    """Declare one Win32 prototype so 64-bit handles are never cut to a C int."""
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = list(argtypes)


if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _bind(_user32, "EnumWindows", wt.BOOL, _WNDENUMPROC, wt.LPARAM)
    _bind(_user32, "GetWindowTextLengthW", ctypes.c_int, wt.HWND)
    _bind(_user32, "GetWindowTextW", ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    _bind(_user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD))
    _bind(_user32, "IsWindow", wt.BOOL, wt.HWND)
    _bind(_user32, "IsWindowVisible", wt.BOOL, wt.HWND)
    _bind(_user32, "GetForegroundWindow", wt.HWND)
    _bind(_user32, "SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
          ctypes.c_int, ctypes.c_int, wt.UINT)
    _bind(_user32, "ShowWindow", wt.BOOL, wt.HWND, ctypes.c_int)
    _bind(_user32, "PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _bind(_user32, "MonitorFromWindow", wt.HANDLE, wt.HWND, wt.DWORD)
    _bind(_user32, "GetMonitorInfoW", wt.BOOL, wt.HANDLE, ctypes.POINTER(_MONITORINFO))

    _bind(_kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
    _bind(_kernel32, "CreateToolhelp32Snapshot", wt.HANDLE, wt.DWORD, wt.DWORD)
    _bind(_kernel32, "Process32FirstW", wt.BOOL, wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _bind(_kernel32, "Process32NextW", wt.BOOL, wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _bind(_kernel32, "SearchPathW", wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
          wt.LPWSTR, ctypes.POINTER(wt.LPWSTR))
    _bind(_kernel32, "CreateProcessW", wt.BOOL, wt.LPCWSTR, wt.LPWSTR, wt.LPVOID, wt.LPVOID,
          wt.BOOL, wt.DWORD, wt.LPVOID, wt.LPCWSTR, ctypes.POINTER(_STARTUPINFOW),
          ctypes.POINTER(_PROCESS_INFORMATION))
    _bind(_shell32, "ShellExecuteW", ctypes.c_size_t, wt.HWND, wt.LPCWSTR, wt.LPCWSTR,
          wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int)


def _need_windows() -> None:
    if not _IS_WINDOWS or _user32 is None:
        raise WinProcError("UNSUPPORTED_PLATFORM", "winproc tools require Windows")


# ---------------------------------------------------------------------------
# PART 2A - pure helpers: integrity RIDs, exe normalization, exclusion set,
# snap-rect math, pydantic arg models. No Win32 calls below this line.
# ---------------------------------------------------------------------------


def normalize_exe(exe: Any) -> str:
    """Lowercase basename without extension; empty string for junk input."""
    if not isinstance(exe, str) or not exe:
        return ""
    base = exe.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[: -len(".exe")]
    return base


_EXCLUDE: set[str] = set()


def exclude_proc(names: Any = None, mode: str = "add") -> set[str]:
    """Maintain the executable exclusion set used by full-mode snapshot walks."""
    global _EXCLUDE
    if mode == "clear":
        _EXCLUDE = set()
        return set(_EXCLUDE)
    items = names if isinstance(names, (list, tuple, set)) else [names]
    normed = {normalize_exe(n) for n in items}
    normed.discard("")
    if mode == "replace":
        _EXCLUDE = normed
    else:  # "add" (and any unknown mode is fail-safe additive)
        _EXCLUDE |= normed
    return set(_EXCLUDE)


def clear_exclusions() -> None:
    _EXCLUDE.clear()


def excluded_names() -> set[str]:
    return set(_EXCLUDE)


def is_excluded(exe: Any) -> bool:
    normed = normalize_exe(exe)
    return bool(normed) and normed in _EXCLUDE


def filter_excluded(items: Any, key: str = "exe") -> list:
    """Drop dicts whose ``key`` names an excluded exe; keep the rest."""
    kept = []
    for item in items or []:
        exe = item.get(key) if isinstance(item, Mapping) else None
        if not is_excluded(exe):
            kept.append(item)
    return kept


def _snap_rect(work: tuple[int, int, int, int], side: str) -> tuple[int, int, int, int]:
    """Split a work area ``(x, y, w, h)`` into a snap half/quarter."""
    x, y, w, h = (int(v) for v in work)
    half_w, half_h = w // 2, h // 2
    rects = {
        "left": (x, y, half_w, h),
        "right": (x + half_w, y, half_w, h),
        "top": (x, y, w, half_h),
        "bottom": (x, y + half_h, w, half_h),
        "topleft": (x, y, half_w, half_h),
        "topright": (x + half_w, y, half_w, half_h),
        "bottomleft": (x, y + half_h, half_w, half_h),
        "bottomright": (x + half_w, y + half_h, half_w, half_h),
    }
    return rects[side]


# ---------------------------------------------------------------------------
# pydantic arg models (one per tool; ValidationError surfaces bad calls)
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


class WinMoveArgs(BaseModel):
    hwnd: int
    x: int
    y: int


class WinResizeArgs(BaseModel):
    hwnd: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class WinSnapArgs(BaseModel):
    hwnd: int
    side: Literal["left", "right", "top", "bottom", "topleft", "topright", "bottomleft", "bottomright"]


class HwndArgs(BaseModel):
    hwnd: int


class OpenDefaultArgs(BaseModel):
    target: str = Field(description="file path (must exist) or http(s) URL")
    cwd: str | None = None


class AppLaunchArgs(BaseModel):
    app: str | None = None
    name: str | None = None  # alias of app (Start-menu/toolbox name)
    path: str | None = None
    argv: list[str] = Field(default_factory=list)
    cwd: str | None = None
    timeout_ms: int = 5000

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if bool(self.app or self.name) == bool(self.path):
            raise ValueError("give exactly one of app / path")
        return self

    def effective_target(self) -> str:
        return self.path or self.app or self.name or ""


class ExcludeProcArgs(BaseModel):
    names: list[str] | None = None
    mode: str = "add"


# ---------------------------------------------------------------------------
# PART 2B - live Win32 operations, tool handlers, TOOLS registry table.
# ---------------------------------------------------------------------------
import subprocess as _subprocess

def _process_snapshot() -> list[dict]:
    """[{pid, ppid, exe}] via Toolhelp32 (dwSize reset before every Next)."""
    _need_windows()
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1, 0xFFFFFFFFFFFFFFFF, None):
        raise WinProcError("SNAPSHOT_FAILED", f"process snapshot failed: {_win32_message()}")
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not _kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            raise WinProcError("SNAPSHOT_FAILED", f"Process32First failed: {_win32_message()}")
        out = []
        while True:
            out.append(
                {"pid": int(entry.th32ProcessID), "ppid": int(entry.th32ParentProcessID),
                 "exe": str(entry.szExeFile)}
            )
            entry.dwSize = ctypes.sizeof(entry)
            if not _kernel32.Process32NextW(snap, ctypes.byref(entry)):
                break
        return out
    finally:
        _kernel32.CloseHandle(snap)


def _enum_windows_real() -> list[tuple[int, str, int, bool]]:
    """[(hwnd, title, pid, visible)] via EnumWindows (callback never throws)."""
    _need_windows()
    found: list[tuple[int, str, int, bool]] = []

    @_WNDENUMPROC
    def _cb(hwnd, _param):  # noqa: ANN001, ANN202
        try:
            length = _user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            if not title:
                return True
            pid = wt.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            visible = bool(_user32.IsWindowVisible(hwnd))
            if visible:
                found.append((int(hwnd), title, int(pid.value), True))
        except Exception:  # noqa: BLE001 - a dying window must not abort the walk
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return found


def _resolve_app_path(target: str, cwd: str | None = None) -> str | None:
    _need_windows()
    import os.path as _p

    candidates = []
    if _p.isabs(target):
        candidates.append(target)
    else:
        if cwd:
            candidates.append(_p.join(cwd, target))
        buf = ctypes.create_unicode_buffer(32767)
        n = _kernel32.SearchPathW(cwd, target, ".exe", 32767, buf, None)
        if n and n < 32767:
            return buf.value
    for cand in candidates:
        if _p.isfile(cand):
            return cand
        if _p.isfile(cand + ".exe"):
            return cand + ".exe"
    return None


def _create_process(path: str, cmdline: str, cwd: str | None, show_cmd: int) -> dict:
    _need_windows()
    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = _STARTF_USESHOWWINDOW
    si.wShowWindow = show_cmd
    pi = _PROCESS_INFORMATION()
    ok = _kernel32.CreateProcessW(
        path, ctypes.create_unicode_buffer(cmdline), None, None, False,
        _CREATE_UNICODE_ENVIRONMENT, None, cwd, ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        raise WinProcError("LAUNCH_FAILED", f"CreateProcess failed: {_win32_message()}")
    try:
        _kernel32.CloseHandle(pi.hThread)
    except Exception:  # noqa: BLE001
        pass
    return {"pid": int(pi.dwProcessId), "path": path}


def _wait_for_window(pid: int, timeout_ms: int, title: str | None = None) -> int | None:
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while True:
        for hwnd, wtitle, wpid, _vis in _enum_windows_real():
            if wpid == pid and (title is None or title.lower() in (wtitle or "").lower()):
                return hwnd
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _shell_execute(target: str, args: str, cwd: str | None, show: int) -> int:
    _need_windows()
    ret = _shell32.ShellExecuteW(None, "open", target, args or None, cwd, show)
    return int(ret)


# ---------------------------------------------------------------------------
# handlers: single-positional mapping in, plain dict out (errors raise)
# ---------------------------------------------------------------------------


def _args(model_cls, args: Any):
    mapping = dict(args or {})
    return model_cls(**mapping)


def h_list_windows(args: Any) -> dict:
    _args(_NoArgs, args)
    wins = [
        {"hwnd": hwnd, "title": title, "pid": pid, "visible": visible}
        for hwnd, title, pid, visible in _enum_windows_real()
    ]
    return {"count": len(wins), "windows": wins}


def h_exclude_proc(args: Any) -> dict:
    m = _args(ExcludeProcArgs, args)
    current = exclude_proc(m.names, m.mode)
    return {"ok": True, "excluded": sorted(current)}


def _require_window(hwnd: int) -> None:
    _need_windows()
    if not _user32.IsWindow(hwnd):
        raise WinProcError("NOT_FOUND", f"hwnd {hwnd} is not a live window")


def h_win_move(args: Any) -> dict:
    m = _args(WinMoveArgs, args)
    _require_window(m.hwnd)
    _user32.SetWindowPos(m.hwnd, _HWND_TOP, m.x, m.y, 0, 0, _SWP_NOSIZE | _SWP_NOZORDER)
    return {"ok": True, "hwnd": m.hwnd, "x": m.x, "y": m.y}


_MONITOR_DEFAULTTONEAREST = 2


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the work area of the monitor that holds ``hwnd``."""
    monitor = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise WinProcError("MONITOR_UNKNOWN", f"no monitor for hwnd {hwnd}: {_win32_message()}")
    work = info.rcWork
    return (work.left, work.top, work.right - work.left, work.bottom - work.top)


def h_win_resize(args: Any) -> dict:
    m = _args(WinResizeArgs, args)
    _require_window(m.hwnd)
    _user32.SetWindowPos(m.hwnd, _HWND_TOP, 0, 0, m.width, m.height, _SWP_NOMOVE | _SWP_NOZORDER)
    return {"ok": True, "hwnd": m.hwnd, "width": m.width, "height": m.height}


def _show_window(hwnd: int, cmd: int, name: str) -> dict:
    _require_window(hwnd)
    _user32.ShowWindow(hwnd, cmd)
    return {"ok": True, "hwnd": hwnd, "state": name}


def h_win_min(args: Any) -> dict:
    return _show_window(_args(HwndArgs, args).hwnd, _SW_MINIMIZE, "minimized")


def h_win_max(args: Any) -> dict:
    return _show_window(_args(HwndArgs, args).hwnd, _SW_MAXIMIZE, "maximized")


def h_win_restore(args: Any) -> dict:
    return _show_window(_args(HwndArgs, args).hwnd, _SW_RESTORE, "restored")


def h_win_snap(args: Any) -> dict:
    m = _args(WinSnapArgs, args)
    _require_window(m.hwnd)
    x, y, w, h = _snap_rect(_work_area(m.hwnd), m.side)
    _user32.SetWindowPos(m.hwnd, _HWND_TOP, x, y, w, h, _SWP_NOZORDER)
    return {"ok": True, "hwnd": m.hwnd, "side": m.side, "rect": [x, y, w, h]}


def h_win_close(args: Any) -> dict:
    """Ask a window to close (WM_CLOSE: the app's own quit path, with its
    save prompts), then confirm the hwnd is really gone."""
    m = _args(HwndArgs, args)
    _require_window(m.hwnd)
    _user32.PostMessageW(m.hwnd, _WM_CLOSE, 0, 0)
    deadline = time.monotonic() + 5.0
    while _user32.IsWindow(m.hwnd):
        if time.monotonic() >= deadline:
            raise WinProcError("CLOSE_UNCONFIRMED",
                               f"hwnd {m.hwnd} ignored WM_CLOSE for 5s")
        time.sleep(0.1)
    return {"ok": True, "hwnd": m.hwnd}


def _foreground_hwnd() -> int | None:
    """The foreground window handle, or None when there is none."""
    _need_windows()
    hwnd = _user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def h_get_active_window(args: Any) -> dict:
    """The window in the foreground: hwnd, title and pid.

    Read-only. Resolved against the live window inventory, so a stale or
    dead handle is NO_FOREGROUND instead of a guess.
    """
    _args(_NoArgs, args)
    hwnd = _foreground_hwnd()
    if hwnd is None:
        raise WinProcError("NO_FOREGROUND", "no foreground window right now")
    for w_hwnd, title, pid, _visible in _enum_windows_real():
        if int(w_hwnd) == hwnd:
            return {"ok": True, "hwnd": hwnd, "title": title, "pid": pid}
    raise WinProcError("NO_FOREGROUND",
                       f"foreground hwnd {hwnd} is not a listed window")


def h_open_default(args: Any) -> dict:
    """Open a file or URL with its default handler (ShellExecute "open").

    Fail-closed: files must exist; URLs are limited to http(s). A shell
    return at or below 32 is OPEN_FAILED, never silent.
    """
    m = _args(OpenDefaultArgs, args)
    target = m.target.strip()
    lowered = target.lower()
    if not lowered.startswith(("http://", "https://")):
        if not os.path.isfile(target) and not os.path.isdir(target):
            raise WinProcError("FILE_NOT_FOUND", f"no file at {target!r}")
    ret = _shell_execute(target, "", m.cwd, _SW_SHOWNORMAL)
    if int(ret) <= 32:
        raise WinProcError("OPEN_FAILED",
                           f"shell could not open {target!r} (code {ret})")
    return {"ok": True, "target": target}


def h_app_launch(args: Any) -> dict:
    m = _args(AppLaunchArgs, args)
    target = m.effective_target()
    resolved = _resolve_app_path(target, m.cwd)
    if resolved is not None:
        argv = [resolved] + list(m.argv or [])
        cmdline = _subprocess.list2cmdline(argv)
        created = _create_process(resolved, cmdline, m.cwd, _SW_SHOWNORMAL)
        hwnd = _wait_for_window(created["pid"], m.timeout_ms)
        return {"ok": True, "pid": created["pid"], "hwnd": hwnd, "path": resolved}
    ret = _shell_execute(target, _subprocess.list2cmdline(list(m.argv or [])), m.cwd, _SW_SHOWNORMAL)
    if int(ret) <= 32:
        raise WinProcError("LAUNCH_FAILED", f"shell could not start {target!r} (code {ret})")
    return {"ok": True, "pid": None, "hwnd": None, "target": target}


def _tool(name: str, description: str, schema, handler) -> dict:
    return {"name": name, "description": description, "schema": schema, "handler": handler}


TOOLS = [
    _tool("list_windows", "List visible titled top-level windows.", _NoArgs, h_list_windows),
    _tool("app_launch", "Launch an app by Start-menu name or exact path.", AppLaunchArgs, h_app_launch),
    _tool("win_move", "Move a window (needs a live hwnd).", WinMoveArgs, h_win_move),
    _tool("win_resize", "Resize a window (needs a live hwnd).", WinResizeArgs, h_win_resize),
    _tool("win_min", "Minimize a window.", HwndArgs, h_win_min),
    _tool("win_max", "Maximize a window.", HwndArgs, h_win_max),
    _tool("win_restore", "Restore a window.", HwndArgs, h_win_restore),
    _tool("win_snap", "Snap a window to a half/quarter of the work area.", WinSnapArgs, h_win_snap),
    _tool("win_close", "Ask a window to close via WM_CLOSE and confirm it is gone.", HwndArgs, h_win_close),
    _tool("get_active_window", "Foreground window: hwnd, title, pid.", _NoArgs, h_get_active_window),
    _tool("open_default", "Open a file/URL with its default handler.", OpenDefaultArgs, h_open_default),
    _tool("exclude_proc", "Add/replace/clear the traversal exclusion set.", ExcludeProcArgs, h_exclude_proc),
]
