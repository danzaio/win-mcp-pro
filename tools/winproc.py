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
import shutil
import struct
import sys
import time
from typing import Any, Callable, Literal, Mapping, TypeVar

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
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_WM_CLOSE = 0x0010
_MONITOR_DEFAULTTONEAREST = 2
_STARTF_USESHOWWINDOW = 0x00000001
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_DWMWA_CLOAKED = 14

# Access rights.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_PROCESS_TERMINATE = 0x0001
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION_CLASS = 18
_TOKEN_ELEVATION = 20
_TOKEN_INTEGRITY_LEVEL = 25
_TH32CS_SNAPPROCESS = 0x00000002

#: Pid 4 is the System process and 0 is the idle process: never terminate.
_PROTECTED_PIDS = (0, 4)

_SNAP_SIDES = (
    "left",
    "right",
    "top",
    "bottom",
    "topleft",
    "topright",
    "bottomleft",
    "bottomright",
)
_SHOW_COMMANDS = {"normal": _SW_SHOWNORMAL, "max": _SW_MAXIMIZE, "min": _SW_MINIMIZE}


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


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wt.WORD),
        ("MaximumLength", wt.WORD),
        ("Buffer", ctypes.c_void_p),
    ]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

_user32 = _kernel32 = _advapi32 = _ntdll = _shell32 = _dwmapi = None

if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    try:  # dwmapi is present since Vista; stay import-safe if it is not
        _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    except OSError:  # pragma: no cover - defensive
        _dwmapi = None

    # user32
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wt.LPARAM]
    _user32.EnumWindows.restype = wt.BOOL
    _user32.IsWindow.argtypes = [wt.HWND]
    _user32.IsWindow.restype = wt.BOOL
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _user32.IsWindowVisible.restype = wt.BOOL
    _user32.IsZoomed.argtypes = [wt.HWND]
    _user32.IsZoomed.restype = wt.BOOL
    _user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
    _user32.GetWindowRect.restype = wt.BOOL
    _user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    _user32.SetWindowPos.restype = wt.BOOL
    _user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wt.BOOL
    _user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    _user32.MonitorFromWindow.restype = wt.HANDLE
    _user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wt.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    if _dwmapi is not None:
        _dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, wt.LPVOID, wt.DWORD]
        _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    # kernel32
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.CloseHandle.restype = wt.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    _kernel32.IsWow64Process.argtypes = [wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    _kernel32.IsWow64Process.restype = wt.BOOL
    _kernel32.ReadProcessMemory.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        wt.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.ReadProcessMemory.restype = wt.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    _kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wt.BOOL
    _kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wt.BOOL
    _kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    _kernel32.TerminateProcess.restype = wt.BOOL
    _kernel32.SearchPathW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.LPWSTR)]
    _kernel32.SearchPathW.restype = wt.DWORD
    _kernel32.CreateProcessW.argtypes = [
        wt.LPWSTR,
        wt.LPWSTR,
        wt.LPVOID,
        wt.LPVOID,
        wt.BOOL,
        wt.DWORD,
        wt.LPVOID,
        wt.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wt.BOOL

    # advapi32 / ntdll / shell32
    _advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    _advapi32.OpenProcessToken.restype = wt.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPVOID,
        wt.DWORD,
        ctypes.POINTER(wt.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wt.BOOL
    _ntdll.NtQueryInformationProcess.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int]
    _shell32.ShellExecuteW.restype = wt.HANDLE


def _need_windows() -> None:
    if not _IS_WINDOWS or _user32 is None:
        raise WinProcError("UNSUPPORTED_PLATFORM", "winproc tools require Windows")


# ---------------------------------------------------------------------------
# PART 2A - pure helpers: integrity RIDs, exe normalization, exclusion set,
# snap-rect math, pydantic arg models. No Win32 calls below this line.
# ---------------------------------------------------------------------------


def _rid_to_integrity(rid: int) -> str:
    """Map a mandatory-label RID to a level name (winnt.h buckets).

    SECURITY_MANDATORY_UNTRUSTED_RID 0x0000, LOW 0x1000, MEDIUM 0x2000,
    HIGH 0x3000, SYSTEM 0x4000, PROTECTED_PROCESS 0x5000. The names match
    server.security.integrity_label so both slices speak one vocabulary.
    """
    if rid >= 0x5000:
        return "protected-process"
    if rid >= 0x4000:
        return "system"
    if rid >= 0x3000:
        return "high"
    if rid >= 0x2000:
        return "medium"
    if rid >= 0x1000:
        return "low"
    return "untrusted"


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


class WinTopmostArgs(BaseModel):
    hwnd: int
    on: bool = True


class CodeGotoArgs(BaseModel):
    path: str
    line: int = Field(default=1, ge=1)
    col: int = Field(default=1, ge=1)
    new_window: bool = False
    timeout_ms: int = Field(default=15000, ge=0)


class PidArgs(BaseModel):
    pid: int


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


class KillArgs(BaseModel):
    pid: int | None = None
    name: str | None = None
    confirm: bool = False


class ExcludeProcArgs(BaseModel):
    names: list[str] | None = None
    mode: str = "add"


# ---------------------------------------------------------------------------
# PART 2B - live Win32 operations, tool handlers, TOOLS registry table.
# ---------------------------------------------------------------------------
import subprocess as _subprocess

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if _IS_WINDOWS else None
_user32 = ctypes.WinDLL("user32", use_last_error=True) if _IS_WINDOWS else None
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True) if _IS_WINDOWS else None
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True) if _IS_WINDOWS else None
_shell32 = ctypes.WinDLL("shell32", use_last_error=True) if _IS_WINDOWS else None

# Keep EnumWindows callbacks alive: a GC'd WINFUNCTYPE crashes the walk.
_enum_callback_refs: list = []


def _query_limited_vm_read(pid: int):
    _need_windows()
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid
    )
    if not handle:
        return None
    return handle


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


def _proc_cmdline(pid: int) -> tuple[str | None, bool, str]:
    """Read a process command line through its PEB (x64 offsets).

    Returns (cmdline|None, ok, reason). Cross-integrity reads fail closed
    with access_denied; Wow64 targets report unverified offsets.
    """
    _need_windows()
    handle = _query_limited_vm_read(pid)
    if not handle:
        return None, False, "access_denied"
    try:
        wow64 = wt.BOOL()
        try:
            if _kernel32.IsWow64Process(handle, ctypes.byref(wow64)) and wow64.value:
                return None, False, "wow64_offsets_unverified"
        except Exception:  # noqa: BLE001 - missing export, keep going native
            pass
        pbi = _PROCESS_BASIC_INFORMATION()
        status = _ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None
        )
        if status != 0:
            return None, False, "peb-query-failed"
        word = ctypes.sizeof(ctypes.c_void_p)
        params_addr = _read_ptr(handle, pbi.PebBaseAddress + 4 * word)
        if not params_addr:
            return None, False, "unreadable-peb"
        # RTL_USER_PROCESS_PARAMETERS +0x70 = CommandLine UNICODE_STRING
        # (u16 length, u16 maxlen, ptr, 4 bytes pad on x64).
        raw = _read_bytes(handle, params_addr + 0x70, 16)
        if not raw:
            return None, False, "unreadable-params"
        length = struct.unpack("<H", raw[:2])[0]
        buf_ptr = struct.unpack("<Q" if word == 8 else "<I", raw[8 if word == 8 else 4:][:word])[0]
        if not length or not buf_ptr:
            return None, False, "empty-command-line"
        data = _read_bytes(handle, buf_ptr, length)
        if not data:
            return None, False, "unreadable-cmdline"
        return data.decode("utf-16-le", "replace"), True, ""
    finally:
        _kernel32.CloseHandle(handle)


def _read_bytes(handle, address: int, size: int) -> bytes | None:
    if not address or size <= 0 or size > 1 << 20:
        return None
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got)
    )
    if not ok or not got.value:
        return None
    return bytes(buf.raw[: got.value])


def _read_ptr(handle, address: int) -> int:
    word = ctypes.sizeof(ctypes.c_void_p)
    raw = _read_bytes(handle, address, word)
    if not raw:
        return 0
    return struct.unpack("<Q" if word == 8 else "<I", raw)[0]


def _token_rid(pid: int) -> int:
    """Mandatory-label RID for a pid (real winnt.h buckets)."""
    _need_windows()
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise WinProcError("NOT_FOUND", f"cannot open pid {pid}: {_win32_message()}")
    try:
        token = wt.HANDLE()
        if not _advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            raise WinProcError("ACCESS_DENIED", f"cannot open token of pid {pid}")
        try:
            size = wt.DWORD(0)
            _advapi32.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value or 64)
            if not _advapi32.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
                raise WinProcError("ACCESS_DENIED", f"cannot read label of pid {pid}")
            label_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))
            sid = label_ptr[0]  # TOKEN_MANDATORY_LABEL.Label.Sid is at offset 0
            rid = struct.unpack("<I", ctypes.string_at(sid + 8, 4))[0]
            return int(rid)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(handle)


def _is_elevated_pid(pid: int) -> bool:
    _need_windows()
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise WinProcError("NOT_FOUND", f"cannot open pid {pid}: {_win32_message()}")
    try:
        token = wt.HANDLE()
        if not _advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
            raise WinProcError("ACCESS_DENIED", f"cannot open token of pid {pid}")
        try:
            elevated = wt.DWORD(0)
            size = wt.DWORD(ctypes.sizeof(elevated))
            if not _advapi32.GetTokenInformation(
                token, _TOKEN_ELEVATION, ctypes.byref(elevated), size, ctypes.byref(size)
            ):
                raise WinProcError("ACCESS_DENIED", f"cannot read elevation of pid {pid}")
            return bool(elevated.value)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(handle)


def _enum_windows_real() -> list[tuple[int, str, int, bool]]:
    """[(hwnd, title, pid, visible)] via EnumWindows (callback never throws)."""
    _need_windows()
    found: list[tuple[int, str, int, bool]] = []

    _CbType = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    @_CbType
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

    _enum_callback_refs.append(_cb)
    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return found


def _terminate_pid(pid: int, code: int = 1) -> bool:
    _need_windows()
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, code))
    finally:
        _kernel32.CloseHandle(handle)


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
        path, cmdline, None, None, False, _CREATE_UNICODE_ENVIRONMENT,
        None, cwd, ctypes.byref(si), ctypes.byref(pi),
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


def h_get_hwnd_pid_path(args: Any) -> dict:
    m = _args(HwndArgs, args)
    _need_windows()
    if not _user32.IsWindow(m.hwnd):
        raise WinProcError("NOT_FOUND", f"hwnd {m.hwnd} is not a live window")
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(m.hwnd, ctypes.byref(pid))
    pid = int(pid.value)
    if not pid:
        raise WinProcError("NOT_FOUND", f"hwnd {m.hwnd} has no owning process")
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    exe, path = "", None
    if handle:
        try:
            size = wt.DWORD(32767)
            buf = ctypes.create_unicode_buffer(32767)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                path = buf.value or None
                exe = (path or "").replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            _kernel32.CloseHandle(handle)
    if not exe:
        raise WinProcError("NOT_FOUND", f"cannot read image of pid {pid}")
    return {"hwnd": m.hwnd, "pid": pid, "exe": exe, "path": path}


def h_proc_tree(args: Any) -> dict:
    _args(_NoArgs, args)
    procs = _process_snapshot()
    by_pid = {p["pid"]: dict(p, children=[]) for p in procs}
    roots = []
    for p in procs:
        parent = by_pid.get(p["ppid"])
        if parent is None or p["ppid"] in (0, p["pid"]):
            roots.append(p["pid"])
        else:
            parent["children"].append(p["pid"])
    nodes = []
    for pid, node in by_pid.items():
        ppid, reparented, ppid_original = node["ppid"], False, None
        if ppid not in by_pid and ppid not in (0, pid):
            # Live-system truth: session processes (csrss/wininit/winlogon)
            # outlive their parents, leaving dangling ppids. Reattach them at
            # the root so the tree invariant holds, keeping the truth around.
            ppid_original, ppid, reparented = ppid, 0, True
        cmdline, ok, _reason = _proc_cmdline(pid)
        nodes.append(
            {"pid": pid, "ppid": ppid, "exe": node["exe"],
             "children": node["children"], "cmdline": cmdline or "",
             "cmdline_ok": ok, "ppid_original": ppid_original,
             "reparented": reparented}
        )
    return {"count": len(nodes), "processes": nodes, "roots": roots}


def h_is_elevated(args: Any) -> dict:
    m = _args(PidArgs, args)
    return {"pid": m.pid, "elevated": _is_elevated_pid(m.pid)}


def h_integrity_level(args: Any) -> dict:
    m = _args(PidArgs, args)
    rid = _token_rid(m.pid)
    return {"pid": m.pid, "rid": rid, "level": _rid_to_integrity(rid)}


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


def _work_area() -> tuple[int, int, int, int]:
    rect = _RECT()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


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
    x, y, w, h = _snap_rect(_work_area(), m.side)
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


def h_win_topmost(args: Any) -> dict:
    """Pin (or unpin) a window above the rest without moving or resizing it."""
    m = _args(WinTopmostArgs, args)
    _require_window(m.hwnd)
    after = _HWND_TOPMOST if m.on else _HWND_NOTOPMOST
    _user32.SetWindowPos(m.hwnd, after, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE)
    return {"ok": True, "hwnd": m.hwnd,
            "state": "topmost" if m.on else "normal"}


def _find_code_cli() -> str | None:
    """The VS Code CLI: PATH first, then its documented install location."""
    found = shutil.which("code")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidate = os.path.join(local, "Programs", "Microsoft VS Code",
                             "bin", "code.cmd")
    if local and os.path.isfile(candidate):
        return candidate
    return None


def h_code_goto(args: Any) -> dict:
    """Open a file at a line in VS Code through its documented CLI.

    ``code --goto path:line:col`` is the editor's own remote control - no
    pixels, no guessing. Fail-closed: the file must exist and the CLI must
    be installed.
    """
    m = _args(CodeGotoArgs, args)
    _need_windows()
    target = os.path.abspath(m.path)
    if not os.path.isfile(target):
        raise WinProcError("FILE_NOT_FOUND", f"no file at {target!r}")
    cli = _find_code_cli()
    if cli is None:
        raise WinProcError("CODE_NOT_FOUND",
                           "VS Code CLI not found (code on PATH or its install dir)")
    goto = f"{target}:{m.line}:{m.col}"
    argv = [cli, "--goto", goto,
            "--new-window" if m.new_window else "--reuse-window"]
    created = _create_process(cli, _subprocess.list2cmdline(argv), None, _SW_SHOWNORMAL)
    hwnd = _wait_for_window(created["pid"], m.timeout_ms)
    return {"ok": True, "pid": created["pid"], "hwnd": hwnd, "path": target}


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


def h_kill_proc(args: Any) -> dict:
    m = _args(KillArgs, args)
    if not m.confirm:
        raise WinProcError("CONFIRM_REQUIRED", "kill_proc needs confirm=True")
    me = os.getpid()
    if m.pid is not None:
        if m.pid == me:
            raise WinProcError("SELF_KILL", "refusing to kill the agent process itself")
        if m.pid in _PROTECTED_PIDS:
            raise WinProcError("PROTECTED_PID", f"refusing to kill protected pid {m.pid}")
        ok = _terminate_pid(m.pid)
        return {"ok": bool(ok), "killed": [{"pid": m.pid}] if ok else []}
    if m.name:
        want = normalize_exe(m.name)
        killed, skipped = [], []
        for proc in _process_snapshot():
            if normalize_exe(proc.get("exe")) != want:
                continue
            if proc["pid"] in (me, *_PROTECTED_PIDS):
                skipped.append(proc["pid"])
                continue
            if _terminate_pid(proc["pid"]):
                killed.append({"pid": proc["pid"]})
        return {"ok": True, "killed": killed, "skipped_protected": skipped}
    raise WinProcError("CONFIRM_REQUIRED", "kill_proc needs confirm=True")


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
    _tool("win_topmost", "Pin/unpin a window above the rest (no move/resize).", WinTopmostArgs, h_win_topmost),
    _tool("code_goto", "Open a file at line:col in VS Code via its CLI.", CodeGotoArgs, h_code_goto),
    _tool("get_hwnd_pid_path", "Owner pid + exe path for a window handle.", HwndArgs, h_get_hwnd_pid_path),
    _tool("proc_tree", "Process tree with command lines.", _NoArgs, h_proc_tree),
    _tool("is_elevated", "Whether a pid runs elevated.", PidArgs, h_is_elevated),
    _tool("integrity_level", "Mandatory-label level of a pid.", PidArgs, h_integrity_level),
    _tool("exclude_proc", "Add/replace/clear the traversal exclusion set.", ExcludeProcArgs, h_exclude_proc),
    _tool("kill_proc", "Terminate by pid/name; confirm-first, never self/0/4.", KillArgs, h_kill_proc),
]
