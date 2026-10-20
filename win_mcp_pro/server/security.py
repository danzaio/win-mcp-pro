"""Security helpers for win-mcp-pro (Task 12).

Three things live here, all import-safe on any platform:

* ``check_uipi``  — UIPI / mandatory integrity check via ctypes. Never blocks,
  never raises to the caller: input to a higher-integrity window comes back as
  a typed ``ELEVATION_REQUIRED`` result instead of a silent failure or hang.
* ``parse_tool_filter`` / ``enforce_filter`` — the ``--tools`` /
  ``--exclude-tools`` allowlist consumed by ``server.registry``.
* ``redact`` — masks secrets before anything reaches the audit log.

MCP stdio rule: nothing here ever writes to stdout.
"""
from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# UIPI / mandatory integrity level (Windows only, ctypes only — no comtypes)
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25  # TOKEN_INFORMATION_CLASS

# SECURITY_MANDATORY_*_RID values, plus the well-known Win10 label.
_INTEGRITY_LABELS = {
    0x0000: "untrusted",
    0x1000: "low",
    0x2000: "medium",
    0x2100: "medium-plus",
    0x3000: "high",
    0x4000: "system",
    0x5000: "protected-process",
}

_RID_NAMES = {
    0x1000: "SECURITY_MANDATORY_LOW_RID",
    0x2000: "SECURITY_MANDATORY_MEDIUM_RID",
    0x2100: "SECURITY_MANDATORY_MEDIUM_PLUS_RID",
    0x3000: "SECURITY_MANDATORY_HIGH_RID",
    0x4000: "SECURITY_MANDATORY_SYSTEM_RID",
    0x5000: "SECURITY_MANDATORY_PROTECTED_PROCESS_RID",
}


def _label(rid: int) -> str:
    return _INTEGRITY_LABELS.get(rid, "unknown-0x%x" % rid)


def integrity_rid(pid: int) -> int:
    """Return the mandatory-integrity RID of ``pid``'s process token.

    Raises OSError when the token cannot be read (process gone, access
    denied). Kept as a standalone function so tests can substitute it.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("invalid pid: %r" % (pid,))
    if sys.platform != "win32":
        raise OSError("integrity check is Windows-only")

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise OSError("OpenProcess(%d) failed: WinError %d"
                      % (pid, ctypes.get_last_error()))
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY,
                                         ctypes.byref(token)):
            raise OSError("OpenProcessToken(%d) failed: WinError %d"
                          % (pid, ctypes.get_last_error()))
        try:
            needed = wintypes.DWORD(0)
            # sizing call: the buffer stays NULL, nothing touches the SID yet
            advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0,
                                         ctypes.byref(needed))
            size = needed.value
            if size <= 0:
                raise OSError("GetTokenInformation sizing failed for pid %d"
                              % pid)
            buf = ctypes.create_string_buffer(size)
            if not advapi32.GetTokenInformation(token, TokenIntegrityLevel, buf,
                                                size, ctypes.byref(needed)):
                raise OSError("GetTokenInformation(%d) failed: WinError %d"
                              % (pid, ctypes.get_last_error()))
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if not sid:
                raise OSError("empty mandatory label for pid %d" % pid)
            count = advapi32.GetSidSubAuthorityCount(sid)
            if not count:
                raise OSError("GetSidSubAuthorityCount failed for pid %d" % pid)
            idx = count.contents.value - 1
            rid_ptr = advapi32.GetSidSubAuthority(sid, idx)
            if not rid_ptr:
                raise OSError("GetSidSubAuthority failed for pid %d" % pid)
            return int(rid_ptr.contents.value)
        finally:
            if token:
                kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(handle)


def integrity_label(pid: int) -> str:
    """Human label of ``pid``'s integrity level ("high", "medium", ...)."""
    try:
        return _label(integrity_rid(pid))
    except (OSError, ValueError):
        return "unknown"


def check_uipi(target_pid):
    """Can this process send input to ``target_pid`` under UIPI?

    Returns ``{"ok": True, "detail": ...}`` or a typed failure:
    ``ELEVATION_REQUIRED`` (target runs at a higher integrity level, so UIPI
    would silently drop the input), ``INTEGRITY_CHECK_FAILED`` (token could
    not be read — fail closed, never send) or ``INVALID_PID``.
    """
    if not isinstance(target_pid, int) or isinstance(target_pid, bool) \
            or target_pid <= 0:
        return {"ok": False, "code": "INVALID_PID",
                "detail": "target_pid must be a positive int, got %r"
                          % (target_pid,)}

    import os
    self_pid = os.getpid()
    try:
        own_rid = integrity_rid(self_pid)
        target_rid = integrity_rid(target_pid)
    except (OSError, ValueError) as exc:
        return {"ok": False, "code": "INTEGRITY_CHECK_FAILED",
                "detail": "could not read integrity level: %s. Refusing to "
                          "send input to pid %d." % (exc, target_pid)}

    if target_rid > own_rid:
        target_name = _RID_NAMES.get(target_rid, "0x%x" % target_rid)
        return {
            "ok": False,
            "code": "ELEVATION_REQUIRED",
            "detail": "target pid %d runs at integrity %s (higher than this "
                      "process at %s), so UIPI blocks its input. Restart as "
                      "admin or use a UIAccess-marked host, or run the target "
                      "at the same integrity level."
                      % (target_pid, target_name, _label(own_rid)),
        }
    return {
        "ok": True,
        "detail": "target pid %d runs at integrity %s, at or below this "
                  "process at %s" % (target_pid, _label(target_rid),
                                     _label(own_rid)),
    }


# ---------------------------------------------------------------------------
# --tools / --exclude-tools filtering
# ---------------------------------------------------------------------------

def parse_tool_filter(argv):
    """Parse ``--tools`` / ``--exclude-tools`` comma lists out of ``argv``.

    Returns ``(allowlist, blocklist)`` where each is a ``set[str]`` or
    ``None`` for "not given, no restriction from this flag". Both flags also
    accept the ``--tools=a,b`` form. Unknown args are ignored (other flags
    belong to the registry/server bootstrap).

    Raises ValueError on a missing value, an empty list, or a repeated flag:
    an empty allowlist must never silently mean "allow everything", and an
    ambiguous CLI must not silently pick a side.
    """
    allow = None
    block = None
    seen = set()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not isinstance(arg, str):
            raise ValueError("argument %d is not a string: %r" % (i, arg))
        flag, sep, inline = arg.partition("=")
        if flag not in ("--tools", "--exclude-tools"):
            i += 1
            continue
        if flag in seen:
            raise ValueError("%s specified more than once" % flag)
        seen.add(flag)
        if sep:
            raw = inline
        else:
            if i + 1 >= len(argv):
                raise ValueError("%s needs a comma-separated value" % flag)
            raw = argv[i + 1]
            i += 1
        names = {part.strip() for part in raw.split(",")} - {""}
        if not names:
            raise ValueError(
                "%s was given an empty tool list; an empty allowlist would "
                "disable every tool" % flag)
        if flag == "--tools":
            allow = names
        else:
            block = names
        i += 1
    return allow, block


def enforce_filter(tool_name, allowlist, blocklist) -> bool:
    """Is ``tool_name`` permitted under the parsed filters?

    Blocklist wins over allowlist. A falsy blocklist restricts nothing.
    ``allowlist=None`` means unrestricted; an empty allowlist set denies
    everything (fail closed).
    """
    if blocklist and tool_name in blocklist:
        return False
    if allowlist is not None:
        return tool_name in allowlist
    return True


# ---------------------------------------------------------------------------
# Secret redaction for audit logs
# ---------------------------------------------------------------------------

_MASKABLE_KEY_PARTS = (
    "key", "token", "secret", "password", "passwd", "passphrase",
    "credential", "auth", "cookie", "session", "signature", "private",
)
MASK = "***"


def _is_sensitive_key(key) -> bool:
    if not isinstance(key, str):
        return False
    k = key.lower()
    return any(part in k for part in _MASKABLE_KEY_PARTS)


def redact(obj):
    """Return a copy of ``obj`` with sensitive values masked.

    Any mapping key whose name contains key/token/secret/password/
    credential/auth/... (case-insensitive) has its value replaced by
    ``MASK``. Walks dicts and lists (incl. tuples); everything else passes
    through unchanged. The input is never mutated.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = MASK if _is_sensitive_key(k) else redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact(v) for v in obj)
    return obj
