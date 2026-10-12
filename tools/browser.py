"""Browser DOM backend (Phase C): isolated Chromium over CDP.

Launches the installed Edge/Chrome with a throwaway ``--user-data-dir``
(never the user's profile), speaks Chrome DevTools Protocol over a local
websocket (``websocket-client``), and exposes DOM-level tools with element
refs: ``browser_open``, ``browser_snapshot``, ``browser_click``,
``browser_fill``, ``browser_read``, ``browser_status``, ``browser_close``.

Safety model:

* Commands go to the browser *we* launched (browser-level CDP session scoped
  to our disposable profile). Attaching to the user's own browser session is
  not offered; the profile lives under the temp dir and is removed on close.
* Clicks are dispatched inside the browser (``Input.dispatchMouseEvent`` at
  the element's box-model center) - no OS-level input, no global
  coordinates, so the desktop is never touched.
* Refs are validated before acting: an unknown ref refuses without touching
  the browser, and a node that no longer resolves is a typed ``STALE_REF``
  refusal, never a click somewhere else.
* Import-safe: ``websocket`` missing means a typed UNAVAILABLE at call time,
  never at import. A browser process we launched is killed on close and via
  an ``atexit`` hook.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from itertools import count
from typing import Any

from pydantic import BaseModel, Field

#: Standard install locations, checked in order.
_BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

_CDP_TIMEOUT = 10.0
_LAUNCH_DEADLINE_S = 20.0
_READY_DEADLINE_S = 15.0
MAX_REFS = 300
MAX_TEXT = 20_000
MAX_REF_STORE = 2_000

_STATE: dict[str, Any] = {}
_REFS: dict[str, dict] = {}
_IDS = count(1)
_REF_SEQ = count(1)


class BrowserError(RuntimeError):
    """Typed refusal from the browser backend."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _refusal(exc: BrowserError) -> dict:
    return {"ok": False, "error": exc.code, "reason": exc.reason}


def _reset() -> None:
    """Drop all module state (tests and close use this)."""
    _STATE.clear()
    _REFS.clear()


# ------------------------------------------------------------------ launch


def _find_browser() -> str | None:
    override = os.environ.get("WINMCP_BROWSER_PATH", "").strip()
    if override and os.path.exists(override):
        return override
    for candidate in _BROWSER_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _profile_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "win-mcp-pro-browser",
                        "profile-%d" % os.getpid())
    os.makedirs(path, exist_ok=True)
    return path


def _launch_args(exe: str, profile: str, headless: bool) -> list[str]:
    args = [
        exe,
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "about:blank",
    ]
    if headless:
        args.insert(1, "--headless=new")
    return args


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass


def _connect_ws(url: str):
    try:
        import websocket  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise BrowserError("UNAVAILABLE",
                           "websocket-client is not installed: %s" % exc) from exc
    try:
        return websocket.create_connection(url, timeout=_CDP_TIMEOUT,
                                           suppress_origin=True)
    except Exception as exc:  # noqa: BLE001
        raise BrowserError("LAUNCH_FAILED", "CDP websocket failed: %s" % exc) from exc


def _ensure_launched(headless: bool = True) -> dict:
    """Start the isolated browser if it is not running; return module state."""
    state = _STATE
    proc = state.get("proc")
    if proc is not None and proc.poll() is None and state.get("ws") is not None:
        return state
    _close_browser()

    exe = _find_browser()
    if exe is None:
        raise BrowserError(
            "NO_BROWSER",
            "no Edge/Chrome found; install one or set WINMCP_BROWSER_PATH",
        )
    profile = _profile_dir()
    try:
        proc = subprocess.Popen(
            _launch_args(exe, profile, headless),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise BrowserError("LAUNCH_FAILED", str(exc)[:200]) from exc

    port_path = os.path.join(profile, "DevToolsActivePort")
    deadline = time.monotonic() + _LAUNCH_DEADLINE_S
    port = None
    ws_path = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            shutil.rmtree(profile, ignore_errors=True)
            raise BrowserError("LAUNCH_FAILED", "browser exited during startup")
        if os.path.exists(port_path):
            try:
                with open(port_path, encoding="utf-8") as handle:
                    lines = [line.strip() for line in handle.read().splitlines()]
                if lines and lines[0].isdigit():
                    port = int(lines[0])
                    ws_path = lines[1] if len(lines) > 1 else ""
                    break
            except OSError:
                pass
        time.sleep(0.1)
    if port is None:
        _kill(proc)
        shutil.rmtree(profile, ignore_errors=True)
        raise BrowserError("LAUNCH_FAILED",
                           "DevTools endpoint did not appear within %.0fs"
                           % _LAUNCH_DEADLINE_S)

    ws = _connect_ws("ws://127.0.0.1:%d%s" % (port, ws_path))
    state.update({"proc": proc, "ws": ws, "port": port, "profile": profile,
                  "headless": bool(headless), "session": None, "target": None})
    atexit.register(_shutdown)
    return state


def _shutdown() -> None:
    try:
        _close_browser()
    except Exception:  # noqa: BLE001 - atexit must never raise
        pass


def _close_browser() -> bool:
    state = _STATE
    closed = bool(state.get("proc") is not None or state.get("ws") is not None)
    ws = state.get("ws")
    if ws is not None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    proc = state.get("proc")
    if proc is not None:
        _kill(proc)
    profile = state.get("profile")
    if profile:
        shutil.rmtree(profile, ignore_errors=True)
    _reset()
    return closed


# ------------------------------------------------------------------ cdp


def _call(method: str, params: dict | None = None, *, session: bool = False,
          timeout: float | None = None) -> dict:
    """One CDP round trip; skips events and stale replies by id."""
    ws = _STATE.get("ws")
    if ws is None:
        raise BrowserError("NO_BROWSER", "browser is not running; call browser_open first")
    message: dict[str, Any] = {"id": next(_IDS), "method": method,
                               "params": params or {}}
    if session:
        session_id = _STATE.get("session")
        if not session_id:
            raise BrowserError("NO_PAGE", "no page session; call browser_open first")
        message["sessionId"] = session_id
    limit = timeout if timeout is not None else _CDP_TIMEOUT
    deadline = time.monotonic() + limit
    try:
        ws.settimeout(limit)
        ws.send(json.dumps(message))
    except Exception as exc:  # noqa: BLE001
        raise BrowserError("CDP_ERROR", "send failed: %s" % exc) from exc
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserError("CDP_TIMEOUT", "%s did not answer in %.0fs" % (method, limit))
        try:
            ws.settimeout(remaining)
            raw = ws.recv()
        except Exception as exc:  # noqa: BLE001
            raise BrowserError("CDP_ERROR", "recv failed: %s" % exc) from exc
        try:
            reply = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(reply, dict) or reply.get("id") != message["id"]:
            continue  # event or reply from an older request
        if "error" in reply:
            error = reply.get("error") or {}
            raise BrowserError("CDP_ERROR", str(error.get("message", error))[:300])
        result = reply.get("result")
        return result if isinstance(result, dict) else {}


def _page_info() -> tuple[str, str]:
    result = _call("Runtime.evaluate",
                   {"expression": "[location.href, document.title]",
                    "returnByValue": True},
                   session=True)
    value = (result.get("result") or {}).get("value")
    if isinstance(value, list) and len(value) >= 2:
        return str(value[0]), str(value[1])
    return "", ""


def _wait_ready(deadline_s: float = _READY_DEADLINE_S) -> str:
    deadline = time.monotonic() + deadline_s
    state = "unknown"
    while time.monotonic() < deadline:
        result = _call("Runtime.evaluate",
                       {"expression": "document.readyState", "returnByValue": True},
                       session=True)
        state = str((result.get("result") or {}).get("value") or "unknown")
        if state == "complete":
            return state
        time.sleep(0.1)
    return state


# ------------------------------------------------------------------ schemas


class OpenArgs(BaseModel):
    url: str = Field(default="about:blank",
                     description="URL to open in a new tab of the isolated browser.")
    headless: bool = Field(default=True,
                           description="Launch without a visible window (first launch only).")


class StatusArgs(BaseModel):
    pass


class SnapshotArgs(BaseModel):
    pass


class ClickArgs(BaseModel):
    ref: str = Field(min_length=1, description="Element ref from browser_snapshot.")


class FillArgs(BaseModel):
    ref: str = Field(min_length=1, description="Element ref from browser_snapshot.")
    text: str = Field(description="Text inserted into the focused element.")


class ReadArgs(BaseModel):
    ref: str | None = Field(default=None,
                            description="Element ref; omit for the whole page body text.")


class CloseArgs(BaseModel):
    pass


# ------------------------------------------------------------------ handlers


def handler_open(args: Any) -> dict:
    m = OpenArgs(**dict(args or {}))
    try:
        _ensure_launched(headless=m.headless)
        created = _call("Target.createTarget", {"url": m.url})
        target = created.get("targetId")
        if not target:
            raise BrowserError("CDP_ERROR", "Target.createTarget returned no targetId")
        attach = _call("Target.attachToTarget", {"targetId": target, "flatten": True})
        session_id = attach.get("sessionId")
        if not session_id:
            raise BrowserError("CDP_ERROR", "attachToTarget returned no sessionId")
        _STATE["target"] = target
        _STATE["session"] = session_id
        _call("Page.enable", {}, session=True)
        ready = _wait_ready()
        url, title = _page_info()
    except BrowserError as exc:
        return _refusal(exc)
    proc = _STATE.get("proc")
    return {"ok": True, "pid": proc.pid if proc is not None else None,
            "page": {"targetId": target, "url": url, "title": title, "ready": ready}}


def handler_status(args: Any) -> dict:
    StatusArgs(**dict(args or {}))
    proc = _STATE.get("proc")
    running = bool(proc is not None and proc.poll() is None)
    return {"ok": True, "running": running, "pid": proc.pid if proc is not None else None,
            "port": _STATE.get("port"), "headless": _STATE.get("headless"),
            "profile": _STATE.get("profile"), "target": _STATE.get("target")}


def handler_snapshot(args: Any) -> dict:
    SnapshotArgs(**dict(args or {}))
    try:
        url, title = _page_info()
        tree = _call("Accessibility.getFullAXTree", {}, session=True)
    except BrowserError as exc:
        return _refusal(exc)
    nodes = tree.get("nodes") or []
    refs: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("ignored"):
            continue
        name = str((node.get("name") or {}).get("value") or "").strip()
        if not name:
            continue
        backend = node.get("backendDOMNodeId")
        if not backend:
            continue
        ref = "b%d" % next(_REF_SEQ)
        role = str((node.get("role") or {}).get("value") or "")
        _REFS[ref] = {"backend": int(backend), "role": role, "name": name}
        refs.append({"ref": ref, "role": role, "name": name})
        if len(refs) >= MAX_REFS:
            break
    while len(_REFS) > MAX_REF_STORE:
        _REFS.pop(next(iter(_REFS)))
    return {"ok": True, "url": url, "title": title, "refs": refs,
            "count": len(refs), "truncated": len(refs) >= MAX_REFS}


def _box_center(ref: str) -> tuple[float, float] | dict:
    entry = _REFS.get(ref)
    if entry is None:
        return {"ok": False, "error": "UNKNOWN_REF",
                "reason": "ref %r is not from a snapshot in this session" % ref}
    try:
        model = _call("DOM.getBoxModel", {"backendNodeId": entry["backend"]},
                      session=True)
    except BrowserError as exc:
        return {"ok": False, "error": "STALE_REF",
                "reason": "ref %r no longer resolves: %s" % (ref, exc.reason)}
    content = ((model.get("model") or {}).get("content") or [])
    if len(content) < 8:
        return {"ok": False, "error": "STALE_REF",
                "reason": "ref %r has no box model" % ref}
    xs = content[0::2]
    ys = content[1::2]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def handler_click(args: Any) -> dict:
    m = ClickArgs(**dict(args or {}))
    center = _box_center(m.ref)
    if isinstance(center, dict):
        return center
    x, y = center
    try:
        for event_type in ("mousePressed", "mouseReleased"):
            _call("Input.dispatchMouseEvent",
                  {"type": event_type, "x": x, "y": y, "button": "left",
                   "clickCount": 1},
                  session=True)
    except BrowserError as exc:
        return _refusal(exc)
    entry = _REFS.get(m.ref) or {}
    return {"ok": True, "ref": m.ref, "at": [x, y],
            "role": entry.get("role"), "name": entry.get("name")}


def handler_fill(args: Any) -> dict:
    m = FillArgs(**dict(args or {}))
    entry = _REFS.get(m.ref)
    if entry is None:
        return {"ok": False, "error": "UNKNOWN_REF",
                "reason": "ref %r is not from a snapshot in this session" % m.ref}
    try:
        _call("DOM.focus", {"backendNodeId": entry["backend"]}, session=True)
    except BrowserError as exc:
        return {"ok": False, "error": "STALE_REF",
                "reason": "ref %r no longer resolves: %s" % (m.ref, exc.reason)}
    try:
        _call("Input.insertText", {"text": m.text}, session=True)
    except BrowserError as exc:
        return _refusal(exc)
    return {"ok": True, "ref": m.ref, "chars": len(m.text)}


def handler_read(args: Any) -> dict:
    m = ReadArgs(**dict(args or {}))
    try:
        if m.ref is None:
            result = _call("Runtime.evaluate",
                           {"expression": "document.body ? document.body.innerText : ''",
                            "returnByValue": True},
                           session=True)
            text = str((result.get("result") or {}).get("value") or "")
        else:
            entry = _REFS.get(m.ref)
            if entry is None:
                return {"ok": False, "error": "UNKNOWN_REF",
                        "reason": "ref %r is not from a snapshot in this session" % m.ref}
            resolved = _call("DOM.resolveNode", {"backendNodeId": entry["backend"]},
                             session=True)
            object_id = (resolved.get("object") or {}).get("objectId")
            if not object_id:
                return {"ok": False, "error": "STALE_REF",
                        "reason": "ref %r no longer resolves" % m.ref}
            result = _call("Runtime.callFunctionOn",
                           {"objectId": object_id,
                            "functionDeclaration":
                                "function(){return this.innerText || this.value "
                                "|| this.textContent || '';}",
                            "returnByValue": True},
                           session=True)
            text = str((result.get("result") or {}).get("value") or "")
    except BrowserError as exc:
        return _refusal(exc)
    truncated = len(text) > MAX_TEXT
    return {"ok": True, "text": text[:MAX_TEXT], "chars": len(text),
            "truncated": truncated}


def handler_close(args: Any) -> dict:
    CloseArgs(**dict(args or {}))
    return {"ok": True, "closed": _close_browser()}


# ------------------------------------------------------------------ tools


def _tool(name: str, description: str, schema, handler) -> dict:
    return {"name": name, "description": description, "schema": schema,
            "handler": handler}


TOOLS = [
    _tool("browser_open",
          "Open a URL in a new tab of the isolated browser (a throwaway "
          "profile under the temp dir; never the user's own session). "
          "Launches Edge/Chrome on first use.",
          OpenArgs, handler_open),
    _tool("browser_status",
          "Report whether the isolated browser is running, its debug port, "
          "profile and current tab. Never launches anything.",
          StatusArgs, handler_status),
    _tool("browser_snapshot",
          "Accessibility-tree snapshot of the current page: refs (b1, b2, ...) "
          "with role and name for clicking/filling/reading. Refs stay valid "
          "while their node lives; a gone node refuses as STALE_REF.",
          SnapshotArgs, handler_snapshot),
    _tool("browser_click",
          "Click the element a snapshot ref points to. The click is dispatched "
          "inside the browser at the element's box center (no OS input); an "
          "unknown or gone ref refuses before anything is dispatched.",
          ClickArgs, handler_click),
    _tool("browser_fill",
          "Focus the element a snapshot ref points to and insert text into it.",
          FillArgs, handler_fill),
    _tool("browser_read",
          "Read the page body text, or one element's text via its ref.",
          ReadArgs, handler_read),
    _tool("browser_close",
          "Close the isolated browser and remove its disposable profile.",
          CloseArgs, handler_close),
]
