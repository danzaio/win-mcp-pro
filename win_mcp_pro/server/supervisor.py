"""Supervised UIA worker (Phase B3).

The supervisor runs the tree-walk tools (``snapshot``, ``find``,
``dump_tree``) in a child process and proxies calls over a JSON-line pipe.
A walk that does not answer within its deadline gets the worker killed and
reported as a typed error while the MCP session stays alive; the next call
respawns a fresh worker. A crash-loop guard stops respawning after
``max_restarts`` spawns inside ``restart_window_s``.

Design rules:

* Input never runs in the worker. A killed worker can never abandon a
  half-sent gesture, and the parent keeps the bookkeeping that releases it.
* Walk calls are read-only: a timeout means "result unknown, zero side
  effects", and no call is retried automatically (no double walks, no
  double actions elsewhere).
* One active call at a time: calls serialize on a lock, so replies can
  never be matched to the wrong request.
* The worker exits on stdin EOF, so a dead supervisor never leaves an
  orphan process behind.

Crash semantics: killing the worker resets its in-process registries. Ids
that carry native identity (``hwnd:runtimeid``) keep resolving through the
parent's bounded re-walk; worker-level state is otherwise gone, which the
typed errors say explicitly.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from itertools import count
from typing import Any

__all__ = ["WORKER_TOOLS", "UiaWorkerSupervisor", "WorkerError"]

#: Tree walks that may block on a broken UIA provider.
WORKER_TOOLS = ("snapshot", "find", "dump_tree")


class WorkerError(RuntimeError):
    """The supervisor itself refused (bad tool, crash loop, ...)."""


def _error(code: str, reason: str, **extra: Any) -> dict:
    result = {"ok": False, "error": code, "reason": reason}
    result.update(extra)
    return result


class UiaWorkerSupervisor:
    """Spawns, proxies to, and supervises the UIA worker process."""

    def __init__(self, *, command: list[str] | None = None, timeout_ms: int = 15_000,
                 max_restarts: int = 3, restart_window_s: float = 60.0) -> None:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 1:
            raise ValueError("timeout_ms must be a positive int")
        if max_restarts < 1:
            raise ValueError("max_restarts must be >= 1")
        if restart_window_s <= 0:
            raise ValueError("restart_window_s must be > 0")
        self._command = list(command) if command else [sys.executable, "-m", "win_mcp_pro.server.worker"]
        self._timeout_s = timeout_ms / 1000.0
        self._max_restarts = int(max_restarts)
        self._window_s = float(restart_window_s)
        self._ids = count(1)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._replies: "queue.Queue[dict | None]" = queue.Queue()
        self._spawn_times: list[float] = []

    # ------------------------------------------------------------- state

    @property
    def process(self) -> subprocess.Popen | None:
        """The live worker process, or None when none is running."""
        return self._proc

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---------------------------------------------------------- lifecycle

    def _spawn(self) -> None:
        now = time.monotonic()
        self._spawn_times = [t for t in self._spawn_times if now - t < self._window_s]
        if len(self._spawn_times) >= self._max_restarts:
            raise WorkerError(
                "crash-loop guard: %d worker spawns within %.0fs; refusing to "
                "respawn until the window passes" % (len(self._spawn_times), self._window_s)
            )
        proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # inherits the server's stderr: worker logs stay auditable
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._proc = proc
        self._spawn_times.append(now)
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        """Forward worker stdout lines into the reply queue; None on EOF."""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._replies.put(message)
        finally:
            self._replies.put(None)

    def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass
        # A killed worker may still have queued replies (or an EOF marker);
        # they belong to a dead generation and must never match a new call.
        while True:
            try:
                self._replies.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        """Kill the worker if it is running (server shutdown)."""
        with self._lock:
            self._kill()

    # -------------------------------------------------------------- calls

    def call(self, tool: str, args: dict) -> Any:
        """Run one walk tool in the worker; returns the tool's JSON result."""
        if tool not in WORKER_TOOLS:
            raise WorkerError("not a worker tool: %r" % (tool,))
        with self._lock:
            return self._call_locked(tool, dict(args or {}))

    def _call_locked(self, tool: str, args: dict) -> Any:
        if not self.alive():
            if self._proc is not None:
                self._kill()  # reap a dead process before respawning
            try:
                self._spawn()
            except WorkerError as exc:
                return _error("WORKER_UNAVAILABLE", str(exc))
            except OSError as exc:
                return _error(
                    "WORKER_UNAVAILABLE",
                    "could not start the worker process: %s" % exc,
                )
        proc = self._proc
        assert proc is not None
        message_id = next(self._ids)
        payload = json.dumps({"id": message_id, "tool": tool, "args": args})
        try:
            proc.stdin.write(payload + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._kill()
            return _error(
                "WORKER_LOST",
                "worker pipe closed before %s was delivered: %s" % (tool, exc),
            )
        deadline = time.monotonic() + self._timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                return _error(
                    "WORKER_TIMEOUT",
                    "%s did not answer within %d ms; the worker was killed and a "
                    "fresh one starts on the next call (read-only call, no side "
                    "effect, nothing retried)" % (tool, int(self._timeout_s * 1000)),
                    deadline_ms=int(self._timeout_s * 1000),
                )
            try:
                message = self._replies.get(timeout=remaining)
            except queue.Empty:
                continue
            if message is None:
                self._kill()
                return _error(
                    "WORKER_LOST",
                    "worker exited before answering %s; nothing was retried" % tool,
                )
            if message.get("id") != message_id:
                continue  # stale reply from an older generation: ignore
            if "result" not in message:
                return _error(
                    "WORKER_BAD_REPLY",
                    "worker reply for %s carried no result" % tool,
                )
            return message["result"]
