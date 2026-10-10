"""win-mcp-pro — system tools (Task 10).

Seven tools that touch the machine itself instead of the UIA tree:

* ``powershell(cmd, timeout_ms)`` — bounded execution. stdout/stderr are
  captured and truncated, and the process is killed once the budget runs out.
* ``fs_list`` / ``fs_read`` / ``fs_write`` — filesystem access. Paths are
  confined to the server's cwd unless ``WINMCP_UNCONFINED=1``, and writes need
  ``confirm=True`` so an agent cannot scribble by accident.
* ``registry_get(path, name)`` — registry reads. There is deliberately no
  registry write tool in this module.
* ``notify(title, msg)`` — a toast through BurntToast when the session has a
  desktop; headless runs get a clean no-op success instead of a failure.
* ``scrape_dom(url_hint)`` — DOM-only browser reading. The real backend lives
  in the browser task; here it is a documented seam (``set_dom_backend``) that
  answers ``NO_DOM_BACKEND`` until something is injected.

Two rules this module follows everywhere:

1. **No logging.** The server's audit hook owns logs; this module never writes
   to stdout (that would corrupt the MCP stdio stream) nor stderr.
2. **No secrets in results.** A powershell command line can carry a token, so
   results report ``cmd_len``, never the command text.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError
from pydantic.aliases import AliasChoices

from tools import ToolDef  # registry contract: ToolDef(...) builds the dict form

# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------

#: stdout/stderr are cut at this many characters, in each direction.
MAX_OUTPUT_CHARS = 20_000

#: Ceiling for fs_read bytes, per call.
MAX_FS_READ_BYTES = 1_048_576

DEFAULT_TIMEOUT_MS = 10_000
MAX_TIMEOUT_MS = 600_000

#: Toasts probe for the BurntToast module first, so a machine without it does
#: not pay a full cold powershell start per notification.
TOAST_TIMEOUT_MS = 5_000

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Same word list the audit redaction uses (server/security.py). A token that
#: embeds one of these is a value we do not echo back.
_MASKABLE_PARTS = (
    "key", "token", "secret", "password", "passwd", "passphrase",
    "credential", "auth", "cookie", "session", "signature", "private",
)
MASK = "***"

#: Shorter tokens are treated as ordinary words ("key" alone would otherwise
#: eat "monkey" out of an unrelated diagnostic).
_MASKABLE_MIN_LEN = 8


# ---------------------------------------------------------------------------
# schemas (pydantic v2)
# ---------------------------------------------------------------------------


class PowerShellArgs(BaseModel):
    """Run a bounded powershell command and capture its output."""

    cmd: str = Field(min_length=1, description="Command body passed to powershell -Command.")
    timeout_ms: int = Field(
        default=DEFAULT_TIMEOUT_MS,
        ge=1,
        le=MAX_TIMEOUT_MS,
        description="Wall-clock budget; the process is killed once it is exceeded.",
    )


class FsListArgs(BaseModel):
    """List one directory (directories first, then case-insensitive name)."""

    path: str = Field(default=".", description="Directory to list; relative paths resolve against cwd.")


class FsReadArgs(BaseModel):
    """Read a text file, truncated at max_bytes."""

    path: str = Field(description="File to read; relative paths resolve against cwd.")
    max_bytes: int = Field(
        default=65_536,
        ge=1,
        le=MAX_FS_READ_BYTES,
        description="Hard cutoff for the returned content.",
    )


class FsWriteArgs(BaseModel):
    """Write a text file. confirm=True is mandatory; there is no default."""

    path: str = Field(description="Target file; relative paths resolve against cwd.")
    content: str = Field(description="Full file content (utf-8, written verbatim).")
    confirm: bool = Field(
        default=False,
        description="Must be True. Writes are the only destructive fs tool here.",
    )


class RegistryGetArgs(BaseModel):
    """Read one registry value. Read-only by design."""

    path: str = Field(description="Registry key path, e.g. HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion.")
    name: Optional[str] = Field(
        default=None,
        description="Value name; empty or omitted reads the (Default) value.",
    )


class NotifyArgs(BaseModel):
    """Show a desktop toast."""

    title: str = Field(min_length=1)
    msg: str = Field(min_length=1)


class ScrapeDomArgs(BaseModel):
    """Read a browser page through the DOM backend, when one is injected."""

    model_config = ConfigDict(populate_by_name=True)

    url_hint: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("url_hint", "hint"),
        description="URL (or a hint to match an open tab) for the injected backend.",
    )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _coerce(model_cls: type[BaseModel], args: Union[BaseModel, dict, None], kwargs: dict) -> BaseModel:
    """Accept handler(model), handler(**model_dump) or handler(**kwargs).

    The server is free to call either way; both end up validated.
    """
    if isinstance(args, model_cls):
        return args
    if args is None:
        raw: dict = {}
    elif isinstance(args, BaseModel):
        raw = args.model_dump()
    elif isinstance(args, dict):
        raw = dict(args)
    else:
        raise TypeError(f"expected {model_cls.__name__}, dict or kwargs, got {type(args).__name__}")
    raw.update(kwargs)
    try:
        return model_cls(**raw)
    except ValidationError as exc:  # pragma: no cover - defensive, schemas guard paths
        raise TypeError(f"invalid arguments for {model_cls.__name__}: {exc}") from exc


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _mask_needles(cmd: str) -> list[str]:
    """What to hide: the whole command, plus its credential-looking words."""
    needles = {cmd.strip()}
    for word in re.split(r"\s+", cmd):
        word = word.strip("\"'")
        if len(word) >= _MASKABLE_MIN_LEN and any(part in word.lower() for part in _MASKABLE_PARTS):
            needles.add(word)
    return sorted(needles, key=len, reverse=True)


def _mask_secret_echo(text: str, cmd: str) -> str:
    """Mask the command text and its secret-looking words in captured output.

    Powershell echoes the offending token back on failure, so anything typed on
    the command line can leave through the pipe. The full command is always
    masked; single words are masked only when they look like credentials (the
    audit redaction's word list), which keeps ordinary diagnostics readable.
    """
    if not text:
        return text
    for needle in _mask_needles(cmd):
        text = text.replace(needle, MASK)
    return text


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _confinement_root() -> Path:
    """Read cwd per call, never at import: tests move the server's cwd."""
    return Path(os.getcwd()).resolve()


def _resolve_confined(raw: str) -> tuple[Optional[Path], Optional[dict]]:
    """Resolve raw against cwd, refusing leaves unless unconfined is on."""
    root = _confinement_root()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return None, {"ok": False, "error": "BAD_PATH", "message": str(exc)}
    if not _env_flag("WINMCP_UNCONFINED"):
        try:
            resolved.relative_to(root)
        except ValueError:
            return None, {
                "ok": False,
                "error": "PATH_NOT_CONFINED",
                "path": str(resolved),
                "root": str(root),
            }
    return resolved, None


# ---------------------------------------------------------------------------
# powershell
# ---------------------------------------------------------------------------


def _run_powershell(cmd: str, timeout_ms: int) -> dict:
    """Bounded run. Returns the raw pieces; the public handler adds the envelope."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return {
            "ok": False,
            "error": "SPAWN_FAILED",
            "message": str(exc),
            "timed_out": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "elapsed_ms": _elapsed_ms(started),
        }

    timed_out = False
    try:
        out, err = proc.communicate(timeout=max(0.05, timeout_ms / 1000.0))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc.pid)
        try:
            proc.kill()  # direct backstop if taskkill never ran
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""

    out = out or ""
    err = err or ""
    return {
        "ok": (not timed_out) and proc.returncode == 0,
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "pid": proc.pid,
        "stdout": out[:MAX_OUTPUT_CHARS],
        "stderr": err[:MAX_OUTPUT_CHARS],
        "stdout_truncated": len(out) > MAX_OUTPUT_CHARS,
        "stderr_truncated": len(err) > MAX_OUTPUT_CHARS,
        "elapsed_ms": _elapsed_ms(started),
    }


def _kill_process_tree(pid: int) -> None:
    """Kill a hung command and anything it spawned; taskkill /T first."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # fall through to the direct kill below


def powershell(args: Union[PowerShellArgs, dict, None] = None, **kwargs: Any) -> dict:
    """Run a powershell command under a hard timeout and capture its output.

    ``ok`` means "finished inside the budget with exit code 0". A command that
    ran but failed is still a successful observation, so it comes back with
    ``ok=False``, the real exit code and its stderr. Timed-out commands report
    ``error="TIMEOUT"`` with ``exit_code=None`` and a truncated stdout.
    ``timed_out`` is always present as a boolean.

    stdout and stderr are masked before they leave: the command string and its
    credential-looking words are replaced with ``***``, so a token typed into
    the command line is not echoed back through the result.
    """
    a = _coerce(PowerShellArgs, args, kwargs)
    res = _run_powershell(a.cmd, a.timeout_ms)
    if res.get("timed_out"):
        res["error"] = "TIMEOUT"
    res["stdout"] = _mask_secret_echo(res["stdout"], a.cmd)
    res["stderr"] = _mask_secret_echo(res["stderr"], a.cmd)
    res["cmd_len"] = len(a.cmd)
    return res


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------


def fs_list(args: Union[FsListArgs, dict, None] = None, **kwargs: Any) -> dict:
    """List a directory: ``{ok, path, entries[{name, kind, size_bytes}], count}``."""
    a = _coerce(FsListArgs, args, kwargs)
    resolved, err = _resolve_confined(a.path)
    if err:
        return err
    assert resolved is not None
    if not resolved.exists():
        return {"ok": False, "error": "NOT_FOUND", "path": str(resolved)}
    if not resolved.is_dir():
        return {"ok": False, "error": "NOT_A_DIRECTORY", "path": str(resolved)}

    entries: list[dict] = []
    try:
        with os.scandir(resolved) as scan:
            for item in scan:
                kind = "other"
                size: Optional[int] = None
                try:
                    if item.is_dir(follow_symlinks=False):
                        kind = "dir"
                    elif item.is_file(follow_symlinks=False):
                        kind = "file"
                        size = item.stat(follow_symlinks=False).st_size
                except OSError:
                    kind = "other"
                entries.append({"name": item.name, "kind": kind, "size_bytes": size})
    except OSError as exc:
        return {"ok": False, "error": "IO_ERROR", "path": str(resolved), "message": str(exc)}

    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    return {"ok": True, "path": str(resolved), "entries": entries, "count": len(entries)}


def fs_read(args: Union[FsReadArgs, dict, None] = None, **kwargs: Any) -> dict:
    """Read a text file under a byte cap.

    Returns ``truncated=True`` when the file is bigger than ``max_bytes``, and
    ``size_bytes`` with the real size on disk so the caller can decide whether
    to read a slice differently. Content is utf-8 with replacement characters,
    never an exception, on odd bytes.
    """
    a = _coerce(FsReadArgs, args, kwargs)
    resolved, err = _resolve_confined(a.path)
    if err:
        return err
    assert resolved is not None
    if not resolved.exists():
        return {"ok": False, "error": "NOT_FOUND", "path": str(resolved)}
    if resolved.is_dir():
        return {"ok": False, "error": "IS_DIRECTORY", "path": str(resolved)}
    try:
        size = resolved.stat().st_size
        with open(resolved, "rb") as handle:
            raw = handle.read(a.max_bytes + 1)
    except OSError as exc:
        return {"ok": False, "error": "IO_ERROR", "path": str(resolved), "message": str(exc)}

    truncated = len(raw) > a.max_bytes
    return {
        "ok": True,
        "path": str(resolved),
        "content": raw[: a.max_bytes].decode("utf-8", errors="replace"),
        "truncated": truncated,
        "size_bytes": size,
        "encoding": "utf-8",
    }


def fs_write(args: Union[FsWriteArgs, dict, None] = None, **kwargs: Any) -> dict:
    """Write a text file, creating parent directories.

    Refuses unless ``confirm=True`` (an agent writing by accident is the one
    destructive thing a system tool can do). Content is written verbatim with
    ``newline=""`` so a roundtrip through fs_read is byte-exact.
    """
    a = _coerce(FsWriteArgs, args, kwargs)
    if not a.confirm:
        return {
            "ok": False,
            "error": "CONFIRM_REQUIRED",
            "path": a.path,
            "message": "fs_write needs confirm=True; nothing was written.",
        }
    resolved, err = _resolve_confined(a.path)
    if err:
        return err
    assert resolved is not None
    data = a.content.encode("utf-8")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "w", encoding="utf-8", newline="") as handle:
            handle.write(a.content)
    except OSError as exc:
        return {"ok": False, "error": "IO_ERROR", "path": str(resolved), "message": str(exc)}
    return {"ok": True, "path": str(resolved), "bytes_written": len(data)}


# ---------------------------------------------------------------------------
# registry (read-only)
# ---------------------------------------------------------------------------

_HIVE_ALIASES = {
    "HKLM": "HKEY_LOCAL_MACHINE",
    "HKEY_LOCAL_MACHINE": "HKEY_LOCAL_MACHINE",
    "HKCU": "HKEY_CURRENT_USER",
    "HKEY_CURRENT_USER": "HKEY_CURRENT_USER",
    "HKCR": "HKEY_CLASSES_ROOT",
    "HKEY_CLASSES_ROOT": "HKEY_CLASSES_ROOT",
    "HKU": "HKEY_USERS",
    "HKEY_USERS": "HKEY_USERS",
    "HKCC": "HKEY_CURRENT_CONFIG",
    "HKEY_CURRENT_CONFIG": "HKEY_CURRENT_CONFIG",
}


def registry_get(args: Union[RegistryGetArgs, dict, None] = None, **kwargs: Any) -> dict:
    """Read one registry value.

    Supports short hive names (HKLM, HKCU, HKCR, HKU, HKCC). Values come back
    JSON-shaped: ints stay ints, REG_BINARY becomes hex, REG_MULTI_SZ becomes a
    list of strings. This module has no registry write tool on purpose.
    """
    a = _coerce(RegistryGetArgs, args, kwargs)
    if sys.platform != "win32":
        return {"ok": False, "error": "NOT_SUPPORTED", "path": a.path}
    try:
        import winreg
    except Exception:  # pragma: no cover - non-Windows interpreters
        return {"ok": False, "error": "NOT_SUPPORTED", "path": a.path}

    raw = a.path.strip().strip('"').rstrip("\\").rstrip(":")
    hive_token, _, subkey = raw.partition("\\")
    hive_name = _HIVE_ALIASES.get(hive_token.strip().upper())
    if hive_name is None:
        return {"ok": False, "error": "BAD_REGISTRY_PATH", "path": raw}
    subkey = subkey.strip("\\")

    handle = None
    try:
        handle = winreg.OpenKey(getattr(winreg, hive_name), subkey)
        value, value_type = winreg.QueryValueEx(handle, a.name or "")
    except FileNotFoundError:
        return {"ok": False, "error": "NOT_FOUND", "path": raw, "name": a.name}
    except PermissionError:
        return {"ok": False, "error": "ACCESS_DENIED", "path": raw, "name": a.name}
    except OSError as exc:
        return {"ok": False, "error": "REGISTRY_ERROR", "path": raw, "message": str(exc)}
    finally:
        if handle is not None:
            try:
                winreg.CloseKey(handle)
            except OSError:
                pass

    return {
        "ok": True,
        "exists": True,
        "path": raw,
        "name": a.name,
        "type": _reg_type_name(winreg, value_type),
        "value": _reg_value(value),
    }


def _reg_type_name(winreg: Any, value_type: int) -> str:
    names = {
        getattr(winreg, name): name
        for name in dir(winreg)
        if name.startswith("REG_") and isinstance(getattr(winreg, name), int)
    }
    return names.get(value_type, f"REG_UNKNOWN_{value_type}")


def _reg_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [str(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# notification
# ---------------------------------------------------------------------------


def _send_toast(title: str, msg: str) -> bool:
    """Show a toast via BurntToast when it is installed. True means it ran."""
    if sys.platform != "win32":
        return False
    probe = _run_powershell(
        "(Get-Module -ListAvailable -Name BurntToast) -ne $null", TOAST_TIMEOUT_MS
    )
    if probe["stdout"].strip().lower() != "true":
        return False
    safe_title = title.replace("'", "''")
    safe_msg = msg.replace("'", "''")
    sent = _run_powershell(
        f"Import-Module BurntToast; New-BurntToastNotification -Text '{safe_title}','{safe_msg}'",
        TOAST_TIMEOUT_MS,
    )
    return bool(sent.get("ok"))


def notify(args: Union[NotifyArgs, dict, None] = None, **kwargs: Any) -> dict:
    """Show a desktop toast.

    Headless (WINMCP_HEADLESS=1, CI, service sessions) is a no-op success
    rather than an error: there is nobody to read a toast. A failing toast
    sender is also swallowed, because a missing BurntToast must not fail the
    caller's step.
    """
    a = _coerce(NotifyArgs, args, kwargs)
    if _env_flag("WINMCP_HEADLESS"):
        return {"ok": True, "delivered": False}
    try:
        delivered = bool(_send_toast(a.title, a.msg))
    except Exception:
        delivered = False
    return {"ok": True, "delivered": delivered}


# ---------------------------------------------------------------------------
# scrape_dom — backend seam
# ---------------------------------------------------------------------------

#: Backend signature: (url_hint: str | None) -> dict
DomBackend = Callable[[Optional[str]], Any]

_dom_lock = threading.Lock()
_dom_backend: Optional[DomBackend] = None


def set_dom_backend(fn: Optional[DomBackend]) -> None:
    """Inject (or clear, with None) the DOM backend used by scrape_dom."""
    global _dom_backend
    with _dom_lock:
        _dom_backend = fn


def get_dom_backend() -> Optional[DomBackend]:
    """The current DOM backend, or None when nothing was injected."""
    with _dom_lock:
        return _dom_backend


def scrape_dom(args: Union[ScrapeDomArgs, dict, None] = None, **kwargs: Any) -> dict:
    """Read a browser page through the injected DOM backend.

    Without a backend the honest answer is ``NO_DOM_BACKEND`` — this module
    never falls back to screenshots or coordinates. The backend's return value
    is passed through untouched; exceptions and non-dict payloads come back as
    typed errors so the caller can retry with a different hint.
    """
    a = _coerce(ScrapeDomArgs, args, kwargs)
    backend = get_dom_backend()
    if backend is None:
        return {"ok": False, "error": "NO_DOM_BACKEND"}
    try:
        data = backend(a.url_hint)
    except Exception as exc:
        # No message: it can carry a URL with a token in it.
        return {"ok": False, "error": "DOM_BACKEND_ERROR", "error_type": type(exc).__name__}
    if data is None:
        return {"ok": False, "error": "DOM_BACKEND_NO_RESULT"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "DOM_BACKEND_BAD_RESULT"}
    return data


# ---------------------------------------------------------------------------
# tool registry contract
# ---------------------------------------------------------------------------

TOOLS = [
    ToolDef(
        name="powershell",
        description=(
            "Run a powershell command under a hard timeout. Captures stdout and "
            "stderr (truncated), kills the process tree on timeout and reports "
            "the exit code. Use for machine state this server has no other tool for."
        ),
        schema=PowerShellArgs,
        handler=powershell,
    ),
    ToolDef(
        name="fs_list",
        description=(
            "List a directory as name/kind/size entries. Paths are confined to the "
            "server's working directory unless WINMCP_UNCONFINED=1."
        ),
        schema=FsListArgs,
        handler=fs_list,
    ),
    ToolDef(
        name="fs_read",
        description=(
            "Read a text file up to a byte cap, reporting the real size and whether "
            "the content was truncated. Same confinement as fs_list."
        ),
        schema=FsReadArgs,
        handler=fs_read,
    ),
    ToolDef(
        name="fs_write",
        description=(
            "Write a text file, creating parent directories. Requires confirm=True "
            "and obeys the same path confinement as fs_list/fs_read."
        ),
        schema=FsWriteArgs,
        handler=fs_write,
    ),
    ToolDef(
        name="registry_get",
        description=(
            "Read one registry value (short hive names accepted: HKLM, HKCU, HKCR, "
            "HKU, HKCC). Read-only; there is no registry write tool."
        ),
        schema=RegistryGetArgs,
        handler=registry_get,
    ),
    ToolDef(
        name="notify",
        description=(
            "Show a desktop toast. Headless sessions get a no-op success, and a "
            "missing toast module never fails the calling step."
        ),
        schema=NotifyArgs,
        handler=notify,
    ),
    ToolDef(
        name="scrape_dom",
        description=(
            "Read a browser page through the DOM backend. Returns NO_DOM_BACKEND "
            "until a backend is injected with set_dom_backend(); never screenshots "
            "and never coordinate fallbacks."
        ),
        schema=ScrapeDomArgs,
        handler=scrape_dom,
    ),
]
