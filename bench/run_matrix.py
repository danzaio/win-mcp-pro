"""Bench matrix (Phase D): fixtures + external oracle + per-task metrics.

Run::

    python bench/run_matrix.py [--out bench/results]

Spawns two disposable WinForms fixtures (a target and a decoy; each logs
every observable effect to its own JSONL file), starts the MCP server over
stdio, and runs 10 deterministic driver-conformance tasks. Effects are
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
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "bench" / "fixtures" / "winforms_probe.ps1"
ALLOWED = ("list_windows,snapshot,find,get_prop,set_value,invoke,zoom,click,"
           "wait_text,wait_enabled,dump_tree,ocr_read")


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
            "-Button2Name", names["alt"], "-StatusName", names["status"]]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _spawn_tk(title: str, log: str, x: int, y: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "bench" / "fixtures" / "tk_probe.py"),
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
                    "alt": f"Bench Alt {suffix}", "status": f"Bench Status {suffix}"}
    decoy_names = {"input": f"Decoy Input {suffix}", "commit": f"Decoy Commit {suffix}",
                   "alt": f"Decoy Alt {suffix}", "status": f"Decoy Status {suffix}"}

    app_target = _spawn_fixture(target_title, target_log, 200, 150, target_names)
    app_decoy = _spawn_fixture(decoy_title, decoy_log, 900, 150, decoy_names)
    app_tk = _spawn_tk(tk_title, tk_log, 1450, 400)

    server_script = ("import runpy, sys\n"
                     f"sys.argv = ['server.main', '--tools', {ALLOWED!r}]\n"
                     "runpy.run_module('server.main', run_name='__main__')\n")
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
                        found = await call("find", {"name": target_title, "kind": "Window"})
                        assert found["ok"], found
                        ctx["window"] = found["element"]["id"]
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
                        assert value["value"] == text, value
                        events = _read_events(target_log)
                        assert any(e.get("event") == "text" and e.get("value") == text
                                   for e in events), events[-5:]
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
                        hit = await call("click", {"x": imgx, "y": imgy,
                                                   "frame": frame["id"], "target": alt})
                        assert hit["ok"], hit
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
                        decoy_window = await find_id(decoy_title, "Window")
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
                        tk_window = await find_id(tk_title, "Window")
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
                        hit = await call("click", {
                            "x": center["x"] - frame["left"],
                            "y": center["y"] - frame["top"],
                            "frame": frame["id"], "target": tk_window})
                        assert hit["ok"], hit
                        assert _wait_for_count(tk_log, "tk_button", 1) == 1, {
                            "hit": hit, "cursor": _under_cursor(),
                            "log": _read_events(tk_log)[-5:],
                        }
                        return "coordinate click on a no-UIA toolkit"

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
                    ]
                    for tid, desc, fn in all_tasks:
                        if only and tid not in only:
                            continue
                        await task(tid, desc, fn)
    finally:
        _kill(app_target)
        _kill(app_decoy)
        _kill(app_tk)
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
