"""Bench matrix (Phase D): fixtures + external oracle + per-task metrics.

Run::

    python bench/run_matrix.py [--out bench/results]

Spawns two disposable WinForms fixtures (a target and a decoy; each logs
every observable effect to its own JSONL file), starts the MCP server over
stdio, and runs 28 deterministic driver-conformance tasks. Effects are
asserted from the fixtures' own logs - the external oracle - never from the
server's reply, and the decoy's log proves zero wrong-target leakage. Writes
JSON + a markdown summary and exits non-zero if any task fails.

Honest scope: driver conformance on this machine, not an LLM task-success
benchmark and not a competitor comparison.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "bench" / "fixtures" / "winforms_probe.ps1"
ALLOWED = ("list_windows,snapshot,find,get_prop,set_value,invoke,zoom,click,"
           "wait_text,wait_enabled,dump_tree,ocr_read,replay,doctor,"
           "cursor_position,double_click,ocr_click,wait_change,move,"
           "menu_click,form_fill,table_extract,choose_file,displays,"
           "win_close,focus,"
           "win_min,win_restore,shortcut,get_active_window,"
           "open_default,type_text,key_hold,win_shot,"
           "get_value")


class SkipTask(Exception):
    """A task that cannot run on this machine (recorded, not failed)."""


# ------------------------------------------------------------------ helpers


def _read_events(path: str) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events


def _count(events: list[dict], kind: str) -> int:
    return sum(1 for event in events if event.get("event") == kind)


def _summarize(results: list[dict]) -> dict:
    return {
        "tasks": len(results),
        "passed": sum(1 for r in results if r["ok"] and not r["skipped"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "skipped": sum(1 for r in results if r["skipped"]),
        "calls": sum(r["calls"] for r in results),
        "ms": round(sum(r["ms"] for r in results), 1),
    }


def _monitor_origin() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)


def _under_cursor() -> dict:
    """Diagnostics for failing click tasks: where the cursor actually is."""
    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    user32 = ctypes.windll.user32
    point = _POINT()
    user32.GetCursorPos(ctypes.byref(point))
    hwnd = user32.WindowFromPoint(point)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    fg = user32.GetForegroundWindow()
    fg_title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(fg, fg_title, 256)
    return {"cursor": [point.x, point.y], "hwnd": hwnd, "pid": pid.value,
            "title": title.value, "foreground": fg_title.value}


def _wait_for_count(path: str, kind: str, wanted: int, timeout: float = 3.0) -> int:
    """Poll the fixture log for an effect.

    Input is delivered through the OS queue, so a click's effect can land a
    few milliseconds after the tool call already returned; reading the
    oracle once immediately would race it.
    """
    deadline = time.monotonic() + timeout
    seen = _count(_read_events(path), kind)
    while seen < wanted and time.monotonic() < deadline:
        time.sleep(0.05)
        seen = _count(_read_events(path), kind)
    return seen


def _spawn_fixture(title: str, log: str, x: int, y: int, names: dict) -> subprocess.Popen:
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        raise SystemExit("powershell.exe not found")
    args = [powershell, "-NoLogo", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
            "-File", str(FIXTURE), "-Title", title, "-Log", log,
            "-X", str(x), "-Y", str(y),
            "-InputName", names["input"], "-Button1Name", names["commit"],
            "-Button2Name", names["alt"], "-StatusName", names["status"],
            "-ClickName", names["click"], "-GridName", names["grid"]]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _spawn_tk(title: str, log: str, x: int, y: int) -> subprocess.Popen:
    return subprocess.Popen(
        # The base interpreter, not the venv launcher: on Windows a venv's python.exe
        # starts the real interpreter as a child, so Popen.pid would not own the window.
        [getattr(sys, "_base_executable", sys.executable), str(ROOT / "bench" / "fixtures" / "tk_probe.py"),
         title, log, str(x), str(y)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ------------------------------------------------------------------ runner


async def _run(only: set[str] | None = None) -> tuple[list[dict], dict]:
    suffix = uuid.uuid4().hex[:8]
    workdir = Path(tempfile.mkdtemp(prefix="win-mcp-bench-"))
    target_log = str(workdir / "target.jsonl")
    decoy_log = str(workdir / "decoy.jsonl")
    tk_log = str(workdir / "tk.jsonl")
    target_title = f"WinMCP Bench Target {suffix}"
    decoy_title = f"WinMCP Bench Decoy {suffix}"
    tk_title = f"WinMCP Bench Tk {suffix}"
    target_names = {"input": f"Bench Input {suffix}", "commit": f"Bench Commit {suffix}",
                    "alt": f"Bench Alt {suffix}", "status": f"Bench Status {suffix}",
                    "click": f"Bench Tap {suffix}", "grid": f"Bench Grid {suffix}"}
    decoy_names = {"input": f"Decoy Input {suffix}", "commit": f"Decoy Commit {suffix}",
                   "alt": f"Decoy Alt {suffix}", "status": f"Decoy Status {suffix}",
                   "click": f"Decoy Tap {suffix}", "grid": f"Decoy Grid {suffix}"}

    app_target = _spawn_fixture(target_title, target_log, 200, 150, target_names)
    app_decoy = _spawn_fixture(decoy_title, decoy_log, 900, 150, decoy_names)
    app_tk = _spawn_tk(tk_title, tk_log, 1450, 400)
    extra_procs: list = []

    server_script = ("import runpy, sys\n"
                     f"sys.argv = ['win_mcp_pro.server.main', '--tools', {ALLOWED!r}]\n"
                     "runpy.run_module('win_mcp_pro.server.main', run_name='__main__')\n")
    params = StdioServerParameters(command=sys.executable, args=["-c", server_script],
                                   cwd=str(ROOT))
    results: list[dict] = []
    ctx: dict = {}
    calls = {"n": 0}
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()

                    async def call(name: str, args: dict | None = None) -> dict:
                        calls["n"] += 1
                        reply = await session.call_tool(name, args or {})
                        assert reply.is_error is False, reply.content
                        return json.loads(reply.content[0].text)

                    async def refusal(name: str, args: dict | None = None) -> dict:
                        """A call the server must refuse: is_error true, typed body."""
                        calls["n"] += 1
                        reply = await session.call_tool(name, args or {})
                        assert reply.is_error is True, reply.content
                        return json.loads(reply.content[0].text)

                    async def task(tid: str, desc: str, fn) -> None:
                        calls_before = calls["n"]
                        started = time.monotonic()
                        try:
                            detail = await fn()
                            ok, skipped = True, False
                        except SkipTask as exc:
                            ok, skipped, detail = True, True, str(exc)
                        except AssertionError as exc:
                            ok, skipped, detail = False, False, str(exc)[:300]
                        except Exception as exc:  # noqa: BLE001
                            ok, skipped, detail = False, False, f"{type(exc).__name__}: {exc}"[:300]
                        results.append({"id": tid, "name": desc, "ok": ok,
                                        "skipped": skipped,
                                        "ms": round((time.monotonic() - started) * 1000, 1),
                                        "calls": calls["n"] - calls_before,
                                        "detail": detail})

                    async def find_id(name: str, kind: str) -> str:
                        found = await call("find", {"name": name, "kind": kind})
                        assert found["ok"], found
                        return found["element"]["id"]

                    async def window_id_for(hwnd: int) -> str:
                        """Bind a top-level window through snapshot roots.

                        ``find`` walks every root's children before checking
                        later roots, so one unrelated stuck provider can stall
                        or truncate it; the depth-1 snapshot is budgeted and
                        skips broken windows.
                        """
                        captured = await call("snapshot", {
                            "mode": "full", "depth": 1, "timeout_ms": 8000})
                        prefix = f"{hwnd}:"
                        for element in captured.get("elements", []):
                            if element.get("kind") == "Window" and element["id"].startswith(prefix):
                                return element["id"]
                        roots = [(e.get("kind"), e.get("id"), e.get("name"))
                                 for e in captured.get("elements", [])][:12]
                        raise AssertionError(
                            f"window {hwnd} not among snapshot roots: {roots}")

                    async def target_window() -> str:
                        return ctx.get("window") or await find_id(target_title, "Window")

                    async def find_in_window(window: str, name: str, kind: str) -> str:
                        """Scoped lookup: child elements come from the window's
                        own snapshot, never a whole-desktop walk (an unrelated
                        stuck provider can poison those)."""
                        captured = await call("snapshot", {
                            "mode": "full", "root": window, "depth": 3,
                            "timeout_ms": 5000})
                        for element in captured.get("elements", []):
                            if element.get("name") == name and element.get("kind") == kind:
                                return element["id"]
                        raise AssertionError(
                            "element %r/%s not found in window snapshot" % (name, kind))

                    # ---- wait for all fixtures to expose their windows
                    deadline = time.monotonic() + 20
                    while True:
                        inventory = await call("list_windows", {})
                        have_target = any(w["title"] == target_title
                                          and w["pid"] == app_target.pid
                                          for w in inventory["windows"])
                        have_decoy = any(w["title"] == decoy_title
                                         and w["pid"] == app_decoy.pid
                                         for w in inventory["windows"])
                        have_tk = any(w["title"] == tk_title and w["pid"] == app_tk.pid
                                      for w in inventory["windows"])
                        if have_target and have_decoy and have_tk:
                            break
                        if time.monotonic() > deadline:
                            raise AssertionError("fixtures did not appear in time")
                        await asyncio.sleep(0.3)

                    async def t01():
                        inventory = await call("list_windows", {})
                        window = next(w for w in inventory["windows"]
                                      if w["title"] == target_title
                                      and w["pid"] == app_target.pid)
                        ctx["window"] = await window_id_for(window["hwnd"])
                        pid = await call("get_prop", {"id": ctx["window"], "prop": "pid"})
                        assert pid["value"] == app_target.pid, pid
                        return f"window={ctx['window']}"

                    async def t02():
                        edit = await find_in_window(await target_window(),
                                                    target_names["input"], "Edit")
                        text = f"bench-{suffix}"
                        written = await call("set_value", {"id": edit, "text": text})
                        assert written["ok"], written
                        value = await call("get_prop", {"id": edit, "prop": "value"})
                        # contains, not equals: physical typing on the fixture
                        # (user interference) must not fail the task
                        assert text in str(value["value"]), value
                        events = _read_events(target_log)
                        assert any(text in str(e.get("value", ""))
                                   for e in events if e.get("event") == "text"), events[-5:]
                        return "set_value readback + fixture logged the change"

                    async def t03():
                        window = await target_window()
                        ctx["commit"] = await find_in_window(window,
                                                             target_names["commit"], "Button")
                        # find the status BEFORE the invoke: the click handler
                        # renames its AccessibleName to "<name> | committed"
                        ctx["status"] = await find_in_window(window,
                                                             target_names["status"], "Text")
                        invoked = await call("invoke", {"id": ctx["commit"]})
                        assert invoked["ok"], invoked
                        assert _wait_for_count(target_log, "button1", 1) == 1, \
                            _read_events(target_log)
                        deadline = time.monotonic() + 3
                        name = {}
                        while time.monotonic() < deadline:
                            name = await call("get_prop", {"id": ctx["status"], "prop": "name"})
                            if "committed" in str(name.get("value", "")).lower():
                                break
                            await asyncio.sleep(0.1)
                        assert "committed" in str(name.get("value", "")).lower(), name
                        return "invoke + oracle button1 event + status renamed"

                    async def t04():
                        status = ctx.get("status") or await find_in_window(
                            await target_window(), target_names["status"], "Text")
                        waited = await call("wait_text", {"s": "committed",
                                                          "id": status, "timeout_ms": 5000})
                        assert waited["ok"], waited
                        return "wait_text saw the committed status"

                    async def t05():
                        commit = ctx.get("commit") or await find_in_window(
                            await target_window(), target_names["commit"], "Button")
                        waited = await call("wait_enabled", {"id": commit,
                                                             "timeout_ms": 5000})
                        assert waited["ok"], {"waited": waited, "id": commit}
                        return "wait_enabled on the commit button"

                    async def t06():
                        window = await target_window()
                        alt = await find_in_window(window, target_names["alt"], "Button")
                        rect = (await call("get_prop", {"id": window, "prop": "rect"}))["value"]
                        brect = (await call("get_prop", {"id": alt, "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        zoom = await call("zoom", {
                            "left": rect["left"] - vx, "top": rect["top"] - vy,
                            "right": rect["right"] - vx, "bottom": rect["bottom"] - vy})
                        assert zoom["ok"], zoom
                        frame = zoom["frame"]
                        imgx = (brect["left"] + brect["right"]) / 2 - frame["left"]
                        imgy = (brect["top"] + brect["bottom"]) / 2 - frame["top"]
                        # one retry: a physical click can interleave and drop
                        # the synthetic one; the oracle still proves the effect
                        for _attempt in range(2):
                            hit = await call("click", {"x": imgx, "y": imgy,
                                                       "frame": frame["id"], "target": alt})
                            assert hit["ok"], hit
                            if _wait_for_count(target_log, "button2", 1, timeout=2.0) >= 1:
                                break
                        assert _wait_for_count(target_log, "button2", 1) == 1, {
                            "hit": hit, "cursor": _under_cursor(),
                            "log": _read_events(target_log)[-5:],
                            "brect": brect, "frame": frame,
                        }
                        return "frame-pinned click reached button2"

                    async def t07():
                        before = len(_read_events(target_log))
                        refused = await call("click", {"x": 10, "y": 10,
                                                       "frame": "f-nope",
                                                       "target": ctx["commit"]})
                        assert refused["error"] == "FRAME_UNKNOWN", refused
                        await asyncio.sleep(0.4)  # let any (wrong) input settle
                        assert len(_read_events(target_log)) == before
                        return "unknown frame refused with zero effect"

                    async def t08():
                        inventory = await call("list_windows", {})
                        decoy = next(w for w in inventory["windows"]
                                     if w["title"] == decoy_title
                                     and w["pid"] == app_decoy.pid)
                        decoy_window = await window_id_for(decoy["hwnd"])
                        drect = (await call("get_prop", {"id": decoy_window, "prop": "rect"}))["value"]
                        decoy_commit = await find_in_window(decoy_window,
                                                            decoy_names["commit"], "Button")
                        dbrect = (await call("get_prop", {"id": decoy_commit, "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        zoom = await call("zoom", {
                            "left": drect["left"] - vx, "top": drect["top"] - vy,
                            "right": drect["right"] - vx, "bottom": drect["bottom"] - vy})
                        assert zoom["ok"], zoom
                        frame = zoom["frame"]
                        imgx = (dbrect["left"] + dbrect["right"]) / 2 - frame["left"]
                        imgy = (dbrect["top"] + dbrect["bottom"]) / 2 - frame["top"]
                        before_target = _count(_read_events(target_log), "button1")
                        refused = await call("click", {"x": imgx, "y": imgy,
                                                       "frame": frame["id"],
                                                       "target": ctx["commit"]})
                        assert refused["error"] in ("TARGET_MISMATCH", "OCCLUDED"), refused
                        await asyncio.sleep(0.5)  # any (wrong) input would land by now
                        assert _count(_read_events(decoy_log), "mouse") == 0, "decoy received input"
                        assert _count(_read_events(decoy_log), "button1") == 0
                        assert _count(_read_events(target_log), "button1") == before_target
                        return "wrong-target refused, decoy log untouched"

                    async def t09():
                        tree = await call("dump_tree", {"id": ctx["window"], "depth": 1})
                        assert tree["ok"], tree
                        kinds = [child["kind"] for child in tree["children"]]
                        assert kinds.count("Button") >= 2, kinds
                        return f"children={kinds}"

                    async def t10():
                        rect = (await call("get_prop", {"id": ctx["window"], "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        ocr = await call("ocr_read", {
                            "left": rect["left"] - vx, "top": rect["top"] - vy,
                            "right": rect["right"] - vx, "bottom": rect["bottom"] - vy})
                        if not ocr.get("ok") and ocr.get("error") == "NO_OCR_LANGUAGE":
                            raise SkipTask("no OCR language pack on this machine")
                        assert ocr["ok"], ocr
                        text = ocr["text"].upper()
                        assert "COMMITTED" in text or "ALT" in text, text[:200]
                        return "ocr read the window text"

                    async def t11():
                        # Tk has no useful UIA tree: bind the window identity,
                        # then click the button by the center the fixture
                        # itself reported (its own external oracle).
                        inventory = await call("list_windows", {})
                        tk_info = next(w for w in inventory["windows"]
                                       if w["title"] == tk_title and w["pid"] == app_tk.pid)
                        tk_window = await window_id_for(tk_info["hwnd"])
                        center = None
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            for event in _read_events(tk_log):
                                if event.get("event") == "button_center":
                                    center = event
                            if center:
                                break
                            await asyncio.sleep(0.2)
                        assert center, "tk fixture never reported its button center"
                        rect = (await call("get_prop", {"id": tk_window, "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        zoom = await call("zoom", {
                            "left": rect["left"] - vx, "top": rect["top"] - vy,
                            "right": rect["right"] - vx, "bottom": rect["bottom"] - vy})
                        assert zoom["ok"], zoom
                        frame = zoom["frame"]
                        for _attempt in range(2):
                            hit = await call("click", {
                                "x": center["x"] - frame["left"],
                                "y": center["y"] - frame["top"],
                                "frame": frame["id"], "target": tk_window})
                            assert hit["ok"], hit
                            if _wait_for_count(tk_log, "tk_button", 1, timeout=2.0) >= 1:
                                break
                        assert _wait_for_count(tk_log, "tk_button", 1) == 1, {
                            "hit": hit, "cursor": _under_cursor(),
                            "log": _read_events(tk_log)[-5:],
                        }
                        return "coordinate click on a no-UIA toolkit"

                    async def t12():
                        # end-to-end replay: step 2 binds step 1's element id
                        window = await target_window()
                        result = await call("replay", {"steps": [
                            {"tool": "dump_tree",
                             "args": {"id": window, "depth": 0},
                             "expect": {"ok": True}},
                            {"tool": "get_prop",
                             "args": {"id": "@id:0", "prop": "pid"},
                             "expect": {"value": app_target.pid}},
                        ]})
                        assert result["ok"] is True, result
                        return "replay: dump_tree -> get_prop by bound id"

                    async def t13():
                        result = await call("doctor", {})
                        assert result["ok"] is True, result
                        return result["summary"]

                    async def t14():
                        pos = await call("cursor_position", {})
                        assert pos["ok"], pos
                        assert isinstance(pos["x"], int) and isinstance(pos["y"], int), pos
                        return f"cursor at ({pos['x']}, {pos['y']})"

                    async def t15():
                        # double_click on the Commit button: the oracle logs
                        # MouseDown/Up per press, so a real double-click is
                        # exactly +2 downs and +2 ups, milliseconds apart
                        # (one SendInput batch, not two separate clicks).
                        window = await target_window()
                        commit = await find_in_window(window, target_names["commit"], "Button")
                        wrect = (await call("get_prop", {"id": window, "prop": "rect"}))["value"]
                        brect = (await call("get_prop", {"id": commit, "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        zoom = await call("zoom", {
                            "left": wrect["left"] - vx, "top": wrect["top"] - vy,
                            "right": wrect["right"] - vx, "bottom": wrect["bottom"] - vy})
                        assert zoom["ok"], zoom
                        frame = zoom["frame"]
                        imgx = (brect["left"] + brect["right"]) / 2 - frame["left"]
                        imgy = (brect["top"] + brect["bottom"]) / 2 - frame["top"]
                        downs_before = _count(_read_events(target_log), "b1_down")
                        ups_before = _count(_read_events(target_log), "b1_up")
                        hit = await call("double_click", {"x": imgx, "y": imgy,
                                                          "frame": frame["id"],
                                                          "target": commit})
                        assert hit["ok"] and hit.get("clicks") == 2, hit
                        assert _wait_for_count(target_log, "b1_down",
                                               downs_before + 2, timeout=3.0) == downs_before + 2
                        assert _wait_for_count(target_log, "b1_up",
                                               ups_before + 2, timeout=3.0) == ups_before + 2
                        downs = [e for e in _read_events(target_log)
                                 if e.get("event") == "b1_down"][-2:]
                        stamps = [datetime.fromisoformat(e["t"]) for e in downs]
                        gap_ms = (stamps[1] - stamps[0]).total_seconds() * 1000
                        assert gap_ms < 1000, gap_ms
                        return f"double-click: 2 press pairs {gap_ms:.0f}ms apart"

                    async def t16():
                        # ocr_click on the big flat TAP HERE label by its text.
                        # Stock button text is not reliably recognized by the
                        # on-box WinOCR (measured: "Alt" never surfaces), so the
                        # fixture carries an OCR-legible target; the mechanism
                        # under test (find -> verify against target -> click ->
                        # oracle event) is identical. The decoy log proves no
                        # leakage across windows.
                        window = await target_window()
                        rect = (await call("get_prop", {"id": window, "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        decoy_before = _count(_read_events(decoy_log), "labelclick")
                        target_before = _count(_read_events(target_log), "labelclick")
                        hit = await call("ocr_click", {
                            "text": "TAP", "target": window,
                            "left": rect["left"] - vx, "top": rect["top"] - vy,
                            "right": rect["right"] - vx, "bottom": rect["bottom"] - vy})
                        if not hit.get("ok") and hit.get("error") == "NO_OCR_LANGUAGE":
                            raise SkipTask("no OCR language pack on this machine")
                        assert hit["ok"], hit
                        assert "TAP" in hit["text"].upper(), hit
                        assert _wait_for_count(target_log, "labelclick",
                                               target_before + 1,
                                               timeout=5.0) == target_before + 1
                        assert _count(_read_events(decoy_log), "labelclick") == decoy_before
                        return "ocr_click hit the TAP label by its text"

                    async def t17():
                        # wait_change, both directions: a static decoy region
                        # times out, and fresh text in the input box fires.
                        inventory = await call("list_windows", {})
                        decoy = next(w for w in inventory["windows"]
                                     if w["title"] == decoy_title
                                     and w["pid"] == app_decoy.pid)
                        decoy_window = await window_id_for(decoy["hwnd"])
                        drect = (await call("get_prop", {"id": decoy_window,
                                                         "prop": "rect"}))["value"]
                        vx, vy = _monitor_origin()
                        quiet = await call("wait_change", {
                            "left": drect["left"] - vx, "top": drect["top"] - vy,
                            "right": drect["right"] - vx, "bottom": drect["bottom"] - vy,
                            "timeout_ms": 700, "threshold": 0.02})
                        assert quiet["ok"] is False, quiet
                        window = await target_window()
                        field = await find_in_window(window, target_names["input"], "Edit")
                        irect = (await call("get_prop", {"id": field, "prop": "rect"}))["value"]
                        waiter_kwargs = {
                            "left": irect["left"] - vx, "top": irect["top"] - vy,
                            "right": irect["right"] - vx, "bottom": irect["bottom"] - vy,
                            "timeout_ms": 8000, "threshold": 0.02}
                        # The waiter runs on its own server connection: two
                        # calls multiplexed over one stdio session serialize,
                        # which would push set_value past the waiter's deadline.
                        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog2:
                            waiter_params = StdioServerParameters(
                                command=sys.executable, args=["-c", server_script],
                                cwd=str(ROOT))
                            async with stdio_client(waiter_params, errlog=errlog2) as streams2:
                                async with ClientSession(*streams2) as session2:
                                    await session2.initialize()

                                    async def call2(name, args=None):
                                        reply = await session2.call_tool(name, args or {})
                                        assert reply.is_error is False, reply.content
                                        return json.loads(reply.content[0].text)

                                    waiter = asyncio.create_task(
                                        call2("wait_change", waiter_kwargs))
                                    await asyncio.sleep(0.4)  # baseline settles first
                                    wrote = await call("set_value", {
                                        "id": field,
                                        "text": f"wait-change {suffix}"})
                                    assert wrote["ok"], wrote
                                    fired = await waiter
                        assert fired["ok"] is True, fired
                        assert fired["changed"] > 0.02, fired
                        return (f"quiet region timed out, fresh text fired "
                                f"in {fired['elapsed_ms']}ms")

                    async def t18():
                        shown = await call("displays", {})
                        assert shown["ok"], shown
                        assert shown["count"] >= 1, shown
                        primaries = [d for d in shown["displays"] if d["primary"]]
                        assert len(primaries) == 1, shown
                        for display in shown["displays"]:
                            assert display["width"] > 0 and display["height"] > 0, display
                        primary = primaries[0]
                        return (f"{shown['count']} display(s), primary "
                                f"{primary['width']}x{primary['height']}")

                    async def t19():
                        window = await target_window()
                        field = await find_in_window(window, target_names["input"], "Edit")
                        text = f"formfill-{suffix}"
                        filled = await call("form_fill", {
                            "fields": [{"id": field, "text": text}]})
                        assert filled["ok"] is True, filled
                        assert filled["fields"][0]["verified"] is True, filled
                        texts = [e.get("value") for e in _read_events(target_log)
                                 if e.get("event") == "text"]
                        assert texts and texts[-1] == text, texts[-5:]
                        return "form_fill wrote one field with readback"

                    async def t20():
                        window = await target_window()
                        grid = None
                        for kind in ("Table", "DataGrid", "Custom"):
                            try:
                                grid = await find_in_window(window, target_names["grid"], kind)
                                break
                            except AssertionError:
                                continue
                        assert grid is not None, "grid not found as Table/DataGrid/Custom"
                        table = await call("table_extract", {"id": grid})
                        assert table["ok"], table
                        assert table["rows"] == [["Key", "Val"], ["alpha", "1"],
                                                 ["beta", "2"], ["gamma", "3"]], table
                        return f"table_extract read {table['shape']}"

                    async def t21():
                        # real File > Open in Notepad, resolved by choose_file;
                        # the window title proves the file landed. The fixtures
                        # are TopMost, so they are minimized first: menu_click
                        # walks the foreground window, and nothing can take the
                        # foreground while a TopMost window stands.
                        async def hwnd_of(title, pid):
                            inventory = await call("list_windows", {})
                            hit = [w for w in inventory["windows"]
                                   if w["title"] == title and w["pid"] == pid]
                            return hit[0]["hwnd"] if hit else None

                        minimized = []
                        for title, pid in ((target_title, app_target.pid),
                                           (decoy_title, app_decoy.pid),
                                           (tk_title, app_tk.pid)):
                            hwnd = await hwnd_of(title, pid)
                            assert hwnd, f"fixture {title} vanished"
                            await call("win_min", {"hwnd": hwnd})
                            minimized.append(hwnd)
                        try:
                            note = workdir / "openme.txt"
                            note.write_text("opened-via-dialog", encoding="utf-8")
                            proc = subprocess.Popen(
                                ["notepad.exe"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            extra_procs.append(proc)
                            try:
                                np_hwnd = None
                                deadline = time.monotonic() + 15
                                while time.monotonic() < deadline:
                                    inventory = await call("list_windows", {})
                                    hit = [w for w in inventory["windows"]
                                           if w["pid"] == proc.pid and w["title"]]
                                    if hit:
                                        np_hwnd = hit[0]["hwnd"]
                                        break
                                    await asyncio.sleep(0.3)
                                assert np_hwnd, "notepad never appeared"
                                np_window = await window_id_for(np_hwnd)
                                # focus lands on the editor, not the window:
                                # SetFocus on a top-level window is a no-op the
                                # tool honestly refuses, while focusing the
                                # Edit activates the window as a side effect.
                                inner = await call("snapshot", {
                                    "mode": "full", "root": np_window,
                                    "depth": 3, "timeout_ms": 8000})
                                edits = [e["id"] for e in inner.get("elements", [])
                                         if e.get("kind") == "Edit"]
                                assert edits, "notepad exposed no Edit control"
                                focused = await call("focus", {"id": edits[0]})
                                assert focused["ok"], focused
                                # Ctrl+O opens the dialog on any UI language;
                                # menu labels ("File" vs "Arquivo") do not.
                                opened_menu = await call("shortcut", {
                                    "keys": "ctrl+o", "target": edits[0]})
                                assert opened_menu["ok"], opened_menu
                                dlg_hwnd = None
                                deadline = time.monotonic() + 10
                                while time.monotonic() < deadline:
                                    inventory = await call("list_windows", {})
                                    hit = [w for w in inventory["windows"]
                                           if w["pid"] == proc.pid
                                           and w["title"] in ("Open", "Abrir")]
                                    if hit:
                                        dlg_hwnd = hit[0]["hwnd"]
                                        break
                                    await asyncio.sleep(0.3)
                                assert dlg_hwnd, "Open dialog never appeared"
                                # Owned dialogs hang under their owner in the
                                # UIA tree, never under the desktop roots.
                                dlg = None
                                deadline = time.monotonic() + 10
                                while time.monotonic() < deadline and dlg is None:
                                    under = await call("snapshot", {
                                        "mode": "full", "root": np_window,
                                        "depth": 3, "timeout_ms": 8000})
                                    for element in under.get("elements", []):
                                        if (element.get("kind") in ("Window", "Dialog")
                                                and (element.get("name") or "")
                                                in ("Open", "Abrir")):
                                            dlg = element["id"]
                                            break
                                    if dlg is None:
                                        await asyncio.sleep(0.5)
                                assert dlg, "Open dialog not under its owner"
                                # confirm-button label is locale-shaped too:
                                # read it off the live dialog, never assume.
                                dlg_tree = await call("snapshot", {
                                    "mode": "full", "root": dlg,
                                    "depth": 3, "timeout_ms": 8000})
                                confirm = next(
                                    e["name"] for e in dlg_tree.get("elements", [])
                                    if e.get("kind") == "Button"
                                    and (e.get("name") or "").casefold() in ("open", "abrir"))
                                chosen = await call("choose_file", {
                                    "dialog": dlg, "path": str(note),
                                    "button": confirm})
                                assert chosen["ok"], chosen
                                deadline = time.monotonic() + 10
                                opened = False
                                while time.monotonic() < deadline:
                                    inventory = await call("list_windows", {})
                                    titles = [w["title"] for w in inventory["windows"]
                                              if w["pid"] == proc.pid]
                                    if (not any(t in ("Open", "Abrir") for t in titles)
                                            and any("openme" in t.lower() for t in titles)):
                                        opened = True
                                        break
                                    await asyncio.sleep(0.3)
                                assert opened, "dialog stayed or file never loaded"
                                return "choose_file opened the file in Notepad"
                            finally:
                                _kill(proc)
                        finally:
                            for hwnd in minimized:
                                restored = await call("win_restore", {"hwnd": hwnd})
                                assert restored["ok"], (hwnd, restored)

                    async def t22():
                        proc = subprocess.Popen(
                            ["notepad.exe"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        extra_procs.append(proc)
                        try:
                            np_hwnd = None
                            deadline = time.monotonic() + 15
                            while time.monotonic() < deadline:
                                inventory = await call("list_windows", {})
                                hit = [w for w in inventory["windows"]
                                       if w["pid"] == proc.pid and w["title"]]
                                if hit:
                                    np_hwnd = hit[0]["hwnd"]
                                    break
                                await asyncio.sleep(0.3)
                            assert np_hwnd, "notepad never appeared"
                            closed = await call("win_close", {"hwnd": np_hwnd})
                            assert closed["ok"], closed
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline:
                                inventory = await call("list_windows", {})
                                if not any(w["pid"] == proc.pid and w["title"]
                                           for w in inventory["windows"]):
                                    break
                                await asyncio.sleep(0.3)
                            else:
                                raise AssertionError("notepad window survived win_close")
                            return "win_close asked, window gone"
                        finally:
                            _kill(proc)

                    async def t23():
                        active = await call("get_active_window", {})
                        assert active["ok"], active
                        assert active["hwnd"] and active["title"] and active["pid"], active
                        return f"foreground is {active['title'][:40]!r}"

                    async def t24():
                        # open_default on a fresh folder: Explorer opens a new
                        # window titled with the folder name; win_close takes
                        # it down. Ghost paths stay fail-closed.
                        folder = workdir / f"open-{suffix}"
                        folder.mkdir(exist_ok=True)
                        before = {w["hwnd"] for w in
                                  (await call("list_windows", {}))["windows"]}
                        opened = await call("open_default", {"target": str(folder)})
                        assert opened["ok"], opened
                        exp_hwnd = None
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            inventory = await call("list_windows", {})
                            hit = [w for w in inventory["windows"]
                                   if w["hwnd"] not in before
                                   and folder.name.lower() in (w["title"] or "").lower()]
                            if hit:
                                exp_hwnd = hit[0]["hwnd"]
                                break
                            await asyncio.sleep(0.4)
                        assert exp_hwnd, "Explorer window never appeared"
                        closed = await call("win_close", {"hwnd": exp_hwnd})
                        assert closed["ok"], closed
                        refused = await refusal("open_default", {
                            "target": str(workdir / "ghost-file.txt")})
                        assert refused["error"] == "FILE_NOT_FOUND", refused
                        return "open_default opened a folder, ghost refused"

                    async def t25():
                        window = await target_window()
                        field = await find_in_window(window, target_names["input"], "Edit")
                        text = f"typed-{suffix}"
                        texts: list = []
                        # One retry: a physical keystroke can interleave and steal the
                        # foreground mid-batch. Typing appends, so every attempt starts
                        # from an empty field, and the oracle log gets time to catch up
                        # (the fixture logs on its own message pump, which lags under load).
                        for _attempt in range(2):
                            cleared = await call("set_value", {"id": field, "text": ""})
                            assert cleared["ok"], cleared
                            typed = await call("type_text", {"text": text, "target": field})
                            assert typed["ok"] and typed["chars"] == len(text), typed
                            deadline = time.monotonic() + 4.0
                            while time.monotonic() < deadline:
                                texts = [e.get("value") for e in _read_events(target_log)
                                         if e.get("event") == "text"]
                                if texts and texts[-1] == text:
                                    break
                                await asyncio.sleep(0.2)
                            if texts and texts[-1] == text:
                                break
                        assert texts and texts[-1] == text, texts[-5:]
                        return "type_text typed the field keystroke by keystroke"

                    async def t26():
                        window = await target_window()
                        field = await find_in_window(window, target_names["input"], "Edit")
                        cleared = await call("set_value", {"id": field, "text": ""})
                        assert cleared["ok"], cleared
                        before = (await call("get_value", {"id": field}))["value"] or ""
                        held = await call("key_hold", {"key": "x", "ms": 800,
                                                       "target": field})
                        assert held["ok"], held
                        after = (await call("get_value", {"id": field}))["value"] or ""
                        assert len(after) > len(before) and after.endswith("x"), (
                            before, after)
                        return f"key_hold grew the field by {len(after) - len(before)}"

                    async def t27():
                        # stepped drag of the target window by its titlebar:
                        # the window rect must travel (nearly) the full path.
                        window = await target_window()
                        rect0 = (await call("get_prop", {"id": window, "prop": "rect"}))["value"]
                        assert rect0["right"] > rect0["left"] and rect0["bottom"] > rect0["top"], rect0
                        vx, vy = _monitor_origin()
                        zoom = await call("zoom", {
                            "left": rect0["left"] - vx, "top": rect0["top"] - vy,
                            "right": rect0["right"] - vx, "bottom": rect0["bottom"] - vy})
                        assert zoom["ok"], zoom
                        frame = zoom["frame"]
                        from_x = (rect0["left"] + rect0["right"]) / 2 - frame["left"]
                        from_y = (rect0["top"] + 15) - frame["top"]
                        to_x, to_y = from_x + 120, from_y
                        dragged = await call("move", {
                            "from_x": from_x, "from_y": from_y,
                            "x": to_x, "y": to_y, "drag": True, "steps": 8,
                            "frame": frame["id"], "target": window})
                        assert dragged["ok"] and dragged.get("steps") == 8, dragged
                        rect1 = (await call("get_prop", {"id": window, "prop": "rect"}))["value"]
                        dx = rect1["left"] - rect0["left"]
                        assert 80 <= dx <= 160, (rect0, rect1)
                        return f"stepped drag moved the window by {dx}px"

                    async def t28():
                        inventory = await call("list_windows", {})
                        target = next(w for w in inventory["windows"]
                                      if w["title"] == target_title
                                      and w["pid"] == app_target.pid)
                        shot = await call("win_shot", {"hwnd": target["hwnd"]})
                        assert shot["ok"], shot
                        assert os.path.isfile(shot["path"]), shot
                        assert shot["frame"]["id"], shot
                        return f"win_shot saved {shot['width']}x{shot['height']}"

                    all_tasks = [
                        ("T01", "find target window (identity)", t01),
                        ("T02", "set_value + oracle readback", t02),
                        ("T03", "invoke + oracle event", t03),
                        ("T04", "wait_text on the status", t04),
                        ("T05", "wait_enabled on the button", t05),
                        ("T06", "frame-pinned click", t06),
                        ("T07", "unknown frame refused", t07),
                        ("T08", "wrong target refused, no leakage", t08),
                        ("T09", "dump_tree children", t09),
                        ("T10", "ocr_read window text", t10),
                        ("T11", "tk click via coordinates (no UIA tree)", t11),
                        ("T12", "replay with id binding", t12),
                        ("T13", "doctor environment check", t13),
                        ("T14", "cursor_position read", t14),
                        ("T15", "double_click two press pairs", t15),
                        ("T16", "ocr_click by visible text", t16),
                        ("T17", "wait_change timeout + fire", t17),
                        ("T18", "displays geometry", t18),
                        ("T19", "form_fill with readback", t19),
                        ("T20", "table_extract grid literals", t20),
                        ("T21", "choose_file real Open dialog", t21),
                        ("T22", "win_close dedicated window", t22),
                        ("T23", "get_active_window shape", t23),
                        ("T24", "open_default folder + ghost", t24),
                        ("T25", "type_text keystrokes + oracle", t25),
                        ("T26", "key_hold grows field", t26),
                        ("T27", "stepped drag moves window", t27),
                        ("T28", "win_shot file + frame", t28),
                    ]
                    for tid, desc, fn in all_tasks:
                        if only and tid not in only:
                            continue
                        await task(tid, desc, fn)
    finally:
        _kill(app_target)
        _kill(app_decoy)
        _kill(app_tk)
        for proc in extra_procs:
            _kill(proc)
    return results, _summarize(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="win-mcp-pro bench matrix (driver conformance)")
    parser.add_argument("--out", default=str(ROOT / "bench" / "results"),
                        help="directory for the JSON + markdown results")
    parser.add_argument("--only", default=None,
                        help="comma-separated task ids to run (debugging)")
    args = parser.parse_args(argv)
    only = set(part.strip() for part in args.only.split(",")) if args.only else None

    started = time.monotonic()
    results, summary = asyncio.run(asyncio.wait_for(_run(only), timeout=300))
    elapsed = round(time.monotonic() - started, 1)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "stamp": stamp,
        "kind": "driver-conformance",
        "notes": ("Effects asserted from the fixtures' own JSONL logs (external oracle); "
                  "the decoy fixture proves zero wrong-target leakage. Not an LLM task "
                  "benchmark and not a competitor comparison."),
        "summary": summary,
        "elapsed_s": elapsed,
        "results": results,
    }
    json_path = out_dir / f"bench-{stamp}.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [f"# win-mcp-pro bench - {stamp}", "",
             f"Tasks: {summary['tasks']} | passed: {summary['passed']} | "
             f"failed: {summary['failed']} | skipped: {summary['skipped']} | "
             f"calls: {summary['calls']} | elapsed: {elapsed}s", "",
             "| Task | Result | ms | calls | Detail |", "| --- | --- | --- | --- | --- |"]
    for row in results:
        mark = "PASS" if row["ok"] and not row["skipped"] else ("SKIP" if row["skipped"] else "FAIL")
        lines.append(f"| {row['id']} {row['name']} | {mark} | {row['ms']} | "
                     f"{row['calls']} | {row['detail']} |")
    md_path = out_dir / f"bench-{stamp}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nJSON: {json_path}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
