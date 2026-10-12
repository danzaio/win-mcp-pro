"""Tests for the supervised UIA worker (Phase B3).

The supervisor runs the tree-walk tools (snapshot/find/dump_tree) in a
child process and proxies calls over a JSON-line pipe. A walk that does not
answer within its deadline gets the worker killed and reported as a typed
error while the session stays alive; the next call respawns. Stub workers
(tiny ``-c`` scripts) drive the failure paths deterministically; one
MCP-route test runs the real worker so the proxy is exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from server import main as server_main, registry
from server.supervisor import WORKER_TOOLS, UiaWorkerSupervisor, WorkerError

ECHO_WORKER = textwrap.dedent("""
    import json, os, sys
    for line in sys.stdin:
        message = json.loads(line)
        result = {"ok": True, "tool": message["tool"], "pid": os.getpid()}
        sys.stdout.write(json.dumps({"id": message["id"], "result": result}) + "\\n")
        sys.stdout.flush()
""")

HANG_ON_BAN_WORKER = textwrap.dedent("""
    import json, os, sys, time
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("tool") == "snapshot":
            time.sleep(60)
        result = {"ok": True, "tool": message["tool"], "pid": os.getpid()}
        sys.stdout.write(json.dumps({"id": message["id"], "result": result}) + "\\n")
        sys.stdout.flush()
""")

DEAD_WORKER = "import sys; sys.exit(0)"

BAD_REPLY_WORKER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        message = json.loads(line)
        sys.stdout.write(json.dumps({"id": message["id"]}) + "\\n")
        sys.stdout.flush()
""")


def _supervisor(script, *, timeout_ms=500, max_restarts=3, window_s=60.0):
    return UiaWorkerSupervisor(
        command=[sys.executable, "-c", script],
        timeout_ms=timeout_ms,
        max_restarts=max_restarts,
        restart_window_s=window_s,
    )


# ---------------------------------------------------------------- happy path


def test_call_round_trip_reuses_one_process():
    sup = _supervisor(ECHO_WORKER)
    try:
        first = sup.call("find", {"name": "x"})
        second = sup.call("snapshot", {"mode": "fast"})
        assert first["ok"] is True and first["tool"] == "find"
        assert second["ok"] is True and second["tool"] == "snapshot"
        assert first["pid"] == second["pid"], "the worker process must be reused"
    finally:
        sup.stop()


def test_concurrent_calls_serialize_and_both_succeed():
    sup = _supervisor(ECHO_WORKER)
    results = []
    try:
        def worker(tool):
            results.append(sup.call(tool, {}))

        threads = [threading.Thread(target=worker, args=("find",)),
                   threading.Thread(target=worker, args=("snapshot",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 2 and all(r["ok"] for r in results)
    finally:
        sup.stop()


# ------------------------------------------------------------------ failures


def test_timeout_kills_worker_within_deadline_and_next_call_respawns():
    sup = _supervisor(HANG_ON_BAN_WORKER, timeout_ms=400)
    try:
        started = time.monotonic()
        result = sup.call("snapshot", {})
        elapsed = time.monotonic() - started
        assert result["ok"] is False and result["error"] == "WORKER_TIMEOUT", result
        assert elapsed < 5, elapsed
        assert sup.process is None, "the hung worker must be killed"
        # the same supervisor recovers on the next call with a fresh process
        fresh = sup.call("find", {})
        assert fresh["ok"] is True, fresh
    finally:
        sup.stop()


def test_worker_that_dies_mid_call_is_typed_without_retry():
    sup = _supervisor(DEAD_WORKER)
    try:
        result = sup.call("find", {})
        assert result["ok"] is False and result["error"] == "WORKER_LOST", result
    finally:
        sup.stop()


def test_crash_loop_guard_stops_respawning():
    sup = _supervisor(DEAD_WORKER, max_restarts=2, window_s=60.0)
    try:
        errors = [sup.call("find", {})["error"] for _ in range(3)]
        assert errors[0] == "WORKER_LOST", errors
        assert errors[1] == "WORKER_LOST", errors
        assert errors[2] == "WORKER_UNAVAILABLE", errors
    finally:
        sup.stop()


def test_malformed_reply_is_typed():
    sup = _supervisor(BAD_REPLY_WORKER)
    try:
        result = sup.call("find", {})
        assert result["ok"] is False and result["error"] == "WORKER_BAD_REPLY", result
    finally:
        sup.stop()


def test_non_worker_tool_is_refused_by_the_supervisor():
    sup = _supervisor(ECHO_WORKER)
    try:
        with pytest.raises(WorkerError):
            sup.call("click", {})
    finally:
        sup.stop()


# ------------------------------------------------------------------ plumbing


def test_cli_uia_worker_flags_are_forwarded(monkeypatch):
    calls = []
    monkeypatch.setattr(registry, "run_server", lambda **kwargs: calls.append(kwargs))
    assert server_main.main(["--uia-worker", "--worker-timeout-ms", "2500"]) == 0
    assert calls[-1]["uia_worker"] is True
    assert calls[-1]["worker_timeout_ms"] == 2500


# ------------------------------------------------------------- MCP route


def _stub_worker_source(flag_path: Path) -> str:
    """A worker that hangs on the first ``find`` and then behaves."""
    return textwrap.dedent(f'''
        import json, os, sys, time
        flag = r"{flag_path}"
        hung_once = os.path.exists(flag)
        if not hung_once:
            open(flag, "w").close()
        for line in sys.stdin:
            message = json.loads(line)
            if not hung_once and message.get("tool") == "find":
                time.sleep(60)  # deterministic hang on the first find
            result = {{"ok": True, "tool": message["tool"], "pid": os.getpid()}}
            sys.stdout.write(json.dumps({{"id": message["id"], "result": result}}) + "\\n")
            sys.stdout.flush()
    ''')


def test_mcp_route_worker_timeout_kills_and_session_survives(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    flag = tmp_path / "hung-once.flag"
    stub = tmp_path / "stub_worker.py"
    stub.write_text(_stub_worker_source(flag), encoding="utf-8")
    server_script = textwrap.dedent(
        f'''
        import runpy
        import sys

        sys.argv = ["server.main", "--tools", "find,list_windows",
                    "--uia-worker", "--worker-timeout-ms", "2000",
                    "--worker-command", {str(stub)!r}]
        runpy.run_module("server.main", run_name="__main__")
        '''
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", server_script],
        cwd=str(project_root),
    )

    async def drive():
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()

                    started = time.monotonic()
                    hung = await session.call_tool("find", {"name": "x"})
                    elapsed = time.monotonic() - started
                    assert hung.is_error is False, hung.content
                    payload = json.loads(hung.content[0].text)
                    assert payload["ok"] is False, payload
                    assert payload["error"] == "WORKER_TIMEOUT", payload
                    assert elapsed < 60, elapsed

                    # the next call respawns a fresh worker; the session was
                    # never blocked by the stuck one
                    fresh = await session.call_tool("find", {"name": "x"})
                    payload = json.loads(fresh.content[0].text)
                    assert payload["ok"] is True, payload
                    assert payload["pid"]

                    # in-process tools still answer after the kill
                    listing = await session.call_tool("list_windows", {})
                    assert listing.is_error is False, listing.content
                    windows = json.loads(listing.content[0].text)
                    assert "windows" in windows, windows

    asyncio.run(asyncio.wait_for(drive(), timeout=120))


def test_worker_module_serves_a_real_bounded_walk():
    """The built-in ``server.worker`` answers a real snapshot (fast mode)."""
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(repo),
    )
    holder: dict = {}

    def read_line():
        holder["line"] = proc.stdout.readline()

    try:
        thread = threading.Thread(target=read_line, daemon=True)
        thread.start()
        proc.stdin.write(
            json.dumps({"id": 1, "tool": "snapshot", "args": {"mode": "fast"}}) + "\n"
        )
        proc.stdin.flush()
        thread.join(timeout=60)
        assert "line" in holder and holder["line"].strip(), "worker did not answer"
        reply = json.loads(holder["line"])
        result = reply["result"]
        assert isinstance(result, dict), "snapshot must cross the pipe as JSON"
        assert "windows" in result and isinstance(result["windows"], list), result
    finally:
        proc.kill()
        proc.wait(timeout=5)
