"""Golden-set runner for win-mcp-pro (Task 14).

The runner executes a set of computer-use tasks against any object that
implements the ``ToolClient`` protocol (duck-typed)::

    class ToolClient(Protocol):
        def call(self, name: str, args: dict | None = None) -> dict: ...

Today the only implementations are fakes (``FakeClient`` here, plus the
ones in ``eval/test_smoke.py``). The same runner drives the real MCP
stdio server later, because the runner only ever does
``client.call(tool_name, args)`` and checks the returned dict.

No screenshots and no real UI automation happen in this module: tasks
declare their intent, and the client is the only thing that touches the
desktop.

Usage::

    python eval/run.py                     # run every task against the fake client
    python eval/run.py --tasks eval/tasks.json
    python eval/run.py --out eval/results.json --min-success 0.8
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_TASKS = EVAL_DIR / "tasks.json"
DEFAULT_OUT = EVAL_DIR / "results.json"

REQUIRED_TASK_KEYS = ("id", "app", "goal", "steps_hint", "expect")

# Checks the runner knows how to evaluate. Kept as data so a task file can
# declare its own expectation without touching runner code.
CHECKS = ("equals", "contains", "matches", "ok")


class TaskFormatError(ValueError):
    """tasks.json is malformed (missing key, unknown check, duplicate id)."""


@runtime_checkable
class ToolClient(Protocol):
    """Anything that can run a tool by name and return a JSON-shaped result.

    Optional: a client may also expose ``begin_task(task)``; the runner calls
    it before each task when present (the real server can use it to reset
    per-task state such as an audit log or a snapshot cache).
    """

    def call(self, name: str, args: dict | None = None) -> dict:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------
# tasks.json loading + validation
# --------------------------------------------------------------------------

def load_tasks(path: str | Path = DEFAULT_TASKS) -> list[dict]:
    """Load and validate a golden set file. Fails loudly on bad shape."""
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = payload["tasks"] if isinstance(payload, Mapping) else payload
    if not isinstance(tasks, list) or not tasks:
        raise TaskFormatError(f"{path}: no tasks found")
    seen: set[str] = set()
    for task in tasks:
        missing = [k for k in REQUIRED_TASK_KEYS if k not in task]
        if missing:
            raise TaskFormatError(f"{path}: task {task.get('id', '?')!r} missing keys {missing}")
        tid = task["id"]
        if tid in seen:
            raise TaskFormatError(f"{path}: duplicate task id {tid!r}")
        seen.add(tid)
        validate_expect(task)
    return list(tasks)


def validate_expect(task: Mapping[str, Any]) -> None:
    expect = task["expect"]
    if not isinstance(expect, Mapping):
        raise TaskFormatError(f"{task['id']}: 'expect' must be an object")
    for key in ("tool", "check"):
        if key not in expect:
            raise TaskFormatError(f"{task['id']}: expect missing {key!r}")
    check = expect["check"]
    if check not in CHECKS:
        raise TaskFormatError(f"{task['id']}: unknown check {check!r} (known: {', '.join(CHECKS)})")
    if check in ("equals", "contains") and "value" not in expect:
        raise TaskFormatError(f"{task['id']}: check {check!r} requires 'value'")
    if check == "matches":
        if "value" not in expect or "regex" not in expect:
            raise TaskFormatError(f"{task['id']}: check 'matches' requires 'value' and 'regex'")
        try:
            re.compile(expect["regex"])
        except re.error as exc:
            raise TaskFormatError(f"{task['id']}: bad regex: {exc}") from exc
    for step in expect.get("setup", []):
        if "tool" not in step:
            raise TaskFormatError(f"{task['id']}: setup step missing 'tool'")


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def _haystack(result: Any) -> str:
    """Best-effort text view of a tool result, for substring/regex checks.

    String leaves are joined raw, without JSON escaping, because a regex
    written against a DOM snippet (``id="login-submit"``) must not have to
    account for backslash-escaped quotes from ``json.dumps``.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        parts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                parts.append(node)
            elif isinstance(node, Mapping):
                for key, val in node.items():
                    parts.append(str(key))
                    walk(val)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)
            else:
                parts.append(str(node))

        walk(result)
        return "\n".join(parts)
    return str(result)


def check_result(expect: Mapping[str, Any], result: Any) -> tuple[bool, str]:
    """Evaluate one expectation against a tool result.

    Returns (ok, reason). The reason is human-readable either way: it is
    what shows up in the markdown table and in results.json.
    """
    kind = expect["check"]

    if kind == "equals":
        want = expect["value"]
        got = result.get("value") if isinstance(result, Mapping) and "value" in result else result
        return (got == want), f"expected {want!r}, got {got!r}"

    if kind == "contains":
        want = expect["value"]
        hay = _haystack(result)
        ok = want in hay
        return ok, (f"found {want!r}" if ok else f"{want!r} not present in result")

    if kind == "matches":
        pattern = expect["regex"]
        hay = _haystack(result)
        m = re.search(pattern, hay)
        return (m is not None), (f"matched /{pattern}/ at {m.start()}" if m else f"/{pattern}/ did not match")

    if kind == "ok":
        ok = result is True or (isinstance(result, Mapping) and result.get("ok") is not False)
        return ok, "call reported ok" if ok else "call reported failure"

    raise TaskFormatError(f"unknown check {kind!r}")  # pragma: no cover - guarded at load


# --------------------------------------------------------------------------
# token accounting
# --------------------------------------------------------------------------

def estimate_tokens(value: Any) -> int:
    """Rough token count (~4 chars per token) for cost comparison vs vision.

    Vision baselines pay per screenshot tile; this number is what a
    text-only run actually spends. Crude on purpose and documented as such;
    the runner sums it over every call in a task (setup calls included).
    """
    return max(1, len(_haystack(value)) // 4)


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def run_task(client: ToolClient, task: Mapping[str, Any], *, measure_time: bool = True) -> dict:
    """Run setup calls, then the probed call, then check the result."""
    begin = getattr(client, "begin_task", None)
    if callable(begin):
        begin(task)

    expect = task["expect"]
    calls: list[tuple[str, dict]] = [(s["tool"], dict(s.get("args") or {})) for s in expect.get("setup", [])]
    calls.append((expect["tool"], dict(expect.get("args") or {})))

    started = time.perf_counter() if measure_time else 0.0
    result: Any = None
    error: str | None = None
    chars = 0  # every result the agent would have to read back, setup included
    for name, args in calls:
        try:
            result = client.call(name, args)
        except Exception as exc:  # a broken client is a failed task, not a crash
            error = f"{name} raised {type(exc).__name__}: {exc}"
            break
        chars += len(_haystack(result))
    ms = (time.perf_counter() - started) * 1000.0 if measure_time else 0.0

    if error is None:
        ok, reason = check_result(expect, result)
    else:
        ok, reason = False, error

    return {
        "id": task["id"],
        "app": task["app"],
        "goal": task["goal"],
        "ok": ok,
        "ms": round(ms, 1),
        "tokens_est": max(1, chars // 4),
        "reason": reason,
    }


def run_eval(client: ToolClient, tasks: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Run every task and return one row per task, in input order."""
    return [run_task(client, task) for task in tasks]


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def success_rate(rows: Iterable[Mapping[str, Any]]) -> float:
    rows = list(rows)
    return (sum(1 for r in rows if r["ok"]) / len(rows)) if rows else 0.0


def format_table(rows: Iterable[Mapping[str, Any]]) -> str:
    """Markdown table (GitHub renders it in Actions logs and in the PR body)."""
    rows = list(rows)
    lines = [
        "| # | task | app | ok | ms | tokens_est | why |",
        "| --: | --- | --- | :---: | --: | --: | --- |",
    ]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"| {i} | {row['id']} | {row['app']} | {'PASS' if row['ok'] else 'FAIL'} "
            f"| {row['ms']} | {row['tokens_est']} | {row['reason'].replace('|', '/')} |"
        )
    passed = sum(1 for r in rows if r["ok"])
    lines.append(f"| | **{passed}/{len(rows)}** | | | | | rate {success_rate(rows):.2f} |")
    return "\n".join(lines)


def write_results(rows: Iterable[Mapping[str, Any]], path: str | Path = DEFAULT_OUT) -> dict:
    """Persist rows as JSON so the CI gate and the README table can consume it."""
    rows = list(rows)
    payload = {
        "success_rate": round(success_rate(rows), 4),
        "passed": sum(1 for r in rows if r["ok"]),
        "total": len(rows),
        "tasks": rows,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


# --------------------------------------------------------------------------
# built-in fake client (smoke only: proves the plumbing, touches no UI)
# --------------------------------------------------------------------------

class FakeClient:
    """Replays canned results. Deterministic, instant, offline.

    Two layers of scripting:

    ``base``    tool-name -> result, shared by every task.
    ``per_task`` task-id -> {tool-name -> result}, applied on ``begin_task``
                so two tasks can probe the same tool and expect different
                answers (the golden set does exactly that with get_value).

    Implements the optional ``begin_task`` from the protocol so the runner
    can swap the active layer without knowing which client it holds.
    """

    def __init__(self, base: Mapping[str, Any] | None = None,
                 per_task: Mapping[str, Mapping[str, Any]] | None = None,
                 *, delay_ms: float = 0.0) -> None:
        self._base = dict(base or {})
        self._per_task = {k: dict(v) for k, v in (per_task or {}).items()}
        self._delay_ms = delay_ms
        self.calls: list[tuple[str, dict]] = []
        self._active: dict[str, Any] = self._base

    def begin_task(self, task: Mapping[str, Any]) -> None:
        self._active = {**self._base, **self._per_task.get(task["id"], {})}

    def call(self, name: str, args: dict | None = None) -> dict:
        self.calls.append((name, dict(args or {})))
        if self._delay_ms:
            time.sleep(self._delay_ms / 1000.0)
        if name not in self._active:
            raise KeyError(f"FakeClient has no scripted result for tool {name!r}")
        return self._active[name]


# Results that make the 12 golden tasks pass, so the smoke run is green.
SMOKE_RESULTS: dict[str, Any] = {
    "app_launch": {"ok": True, "pid": 1234, "hwnd": "0x50654"},
    "list_windows": {"windows": [{"name": "Notepad", "id": "@np:win"},
                                  {"name": "Calculator", "id": "@calc:win"},
                                  {"name": "Public", "id": "@exp:win"},
                                  {"name": "Bluetooth & devices", "id": "@set:win"}]},
    "find": {"id": "@found", "ok": True},
    "set_value": {"ok": True},
    "get_value": {"value": "hello from win-mcp-pro"},
    "get_prop": {"value": "On"},
    "menu_click": {"ok": True, "path": ["File"]},
    "dump_tree": {"elements": [{"name": "History", "kind": "MenuItem"}]},
    "invoke": {"ok": True},
    "toggle": {"ok": True},
    "scroll_into_view": {"ok": True},
    "wait": {"ok": True},
    "wait_text": {"ok": True},
}


def _task_overrides(task: Mapping[str, Any]) -> dict[str, Any]:
    """Per-task answers for the probed tool, derived from the task itself."""
    exp = task["expect"]
    tool, check = exp["tool"], exp["check"]
    if check == "equals" and tool in ("get_value", "get_prop"):
        return {tool: {"value": exp["value"]}}
    if check == "contains" and tool == "get_prop":
        return {tool: {"value": exp["value"]}}
    return {}


def smoke_client(tasks: Iterable[Mapping[str, Any]]) -> FakeClient:
    """A FakeClient whose canned answers satisfy every task in ``tasks``."""
    per_task = {task["id"]: _task_overrides(task) for task in tasks}
    return FakeClient(SMOKE_RESULTS, per_task)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval/run.py",
        description="Run the win-mcp-pro golden set and write eval/results.json.",
    )
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS), help="path to tasks.json")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="where to write results JSON")
    parser.add_argument("--min-success", type=float, default=None,
                        help="exit non-zero if the success rate is below this value (0..1)")
    parser.add_argument("--client", choices=("fake",), default="fake",
                        help="client backend; only the offline fake exists today")
    parser.add_argument("--quiet", action="store_true", help="suppress the markdown table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.min_success is not None and not 0.0 <= args.min_success <= 1.0:
        print("error: --min-success must be between 0 and 1", file=sys.stderr)
        return 2

    try:
        tasks = load_tasks(args.tasks)
    except (OSError, json.JSONDecodeError, TaskFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = smoke_client(tasks)
    rows = run_eval(client, tasks)
    payload = write_results(rows, args.out)

    if not args.quiet:
        print(format_table(rows))
        print()
        print(f"success rate {payload['success_rate']:.2f} ({payload['passed']}/{payload['total']})")
    print(f"wrote {args.out}")

    if args.min_success is not None and payload["success_rate"] < args.min_success:
        print(f"error: success rate {payload['success_rate']:.2f} below {args.min_success:.2f}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
