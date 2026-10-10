"""Smoke tests for the eval runner (Task 14).

Everything here runs against fakes: no Notepad, no Calculator, no Chrome,
no UIA, no screenshots, no network. The point is to prove the runner
plumbing: execute setup steps, probe one tool, check the result, report.

Run:  python -m pytest eval/test_smoke.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parent
if __package__:
    # normal case: pytest imports this as eval.test_smoke
    from eval import run as runner  # noqa: E402
    from eval.run import FakeClient, TaskFormatError  # noqa: E402
else:  # pragma: no cover - flat run: python eval/test_smoke.py
    sys.path.insert(0, str(EVAL_DIR))
    import run as runner  # noqa: E402
    from run import FakeClient, TaskFormatError  # noqa: E402


def make_task(tid: str, expect: dict, app: str = "notepad") -> dict:
    return {
        "id": tid,
        "app": app,
        "goal": f"goal for {tid}",
        "steps_hint": ["find(...)", "set_value(...)"],
        "expect": expect,
    }


# --------------------------------------------------------------------------
# the three sample tasks the plan asks for
# --------------------------------------------------------------------------

SAMPLE_TASKS = [
    make_task("sample-launch", {
        "tool": "list_windows",
        "check": "contains",
        "value": "Notepad",
        "setup": [{"tool": "app_launch", "args": {"name": "notepad"}}],
    }),
    make_task("sample-type", {
        "tool": "get_value",
        "check": "equals",
        "value": "hello from win-mcp-pro",
        "setup": [{"tool": "set_value", "args": {"text": "hello from win-mcp-pro"}}],
    }),
    make_task("sample-probe", {
        "tool": "get_prop",
        "check": "ok",
        "setup": [{"tool": "find", "args": {"name": "Save As"}}],
    }),
]


def sample_client() -> FakeClient:
    """Fake whose scripted answers make all three sample tasks pass."""
    return FakeClient({
        "app_launch": {"ok": True, "pid": 1234, "hwnd": "0x50654"},
        "list_windows": {"windows": [{"name": "Notepad", "id": "@np:win"}]},
        "find": {"id": "@np:saveas", "ok": True},
        "set_value": {"ok": True},
        "get_value": {"value": "hello from win-mcp-pro"},
        "get_prop": {"value": "On"},
    })


def test_three_sample_tasks_pass():
    rows = runner.run_eval(sample_client(), SAMPLE_TASKS)
    assert [r["id"] for r in rows] == ["sample-launch", "sample-type", "sample-probe"]
    assert all(r["ok"] for r in rows), [r["reason"] for r in rows]


def test_rows_have_the_contract_shape():
    rows = runner.run_eval(sample_client(), SAMPLE_TASKS)
    for row in rows:
        assert set(row) == {"id", "app", "goal", "ok", "ms", "tokens_est", "reason"}
        assert isinstance(row["ok"], bool)
        assert row["ms"] >= 0
        assert row["tokens_est"] >= 1
        assert isinstance(row["reason"], str) and row["reason"]


def test_setup_calls_run_before_the_probe():
    client = sample_client()
    runner.run_eval(client, SAMPLE_TASKS)
    names = [name for name, _args in client.calls]
    assert names[:3] == ["app_launch", "list_windows", "set_value"]
    assert names[3] == "get_value"
    assert "find" in names and names[-1] == "get_prop"


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

@pytest.mark.parametrize("check,value,regex,result,expected", [
    ("equals", "42", None, {"value": "42"}, True),
    ("equals", "42", None, {"value": "41"}, False),
    ("equals", 42, None, {"value": 42}, True),
    ("contains", "Notepad", None, {"windows": [{"name": "Notepad"}]}, True),
    ("contains", "Notepad", None, {"windows": [{"name": "Calculator"}]}, False),
    ("matches", None, 'id="login-submit"', '<button id="login-submit">Sign in</button>', True),
    ("matches", None, 'id="login-submit"', '<button id="other">Sign in</button>', False),
    ("ok", None, None, {"ok": True}, True),
    ("ok", None, None, {"ok": False, "error": "ELEVATION_REQUIRED"}, False),
])
def test_check_result(check, value, regex, result, expected):
    expect = {"check": check, "value": value, "regex": regex}
    ok, reason = runner.check_result(expect, result)
    assert ok is expected
    assert reason


def test_equals_accepts_bare_scalar_not_just_value_key():
    ok, _ = runner.check_result({"check": "equals", "value": 42}, 42)
    assert ok is True


def test_failing_check_reports_a_reason():
    rows = runner.run_eval(FakeClient({"get_value": {"value": "wrong"}}),
                           [make_task("t", {"tool": "get_value", "check": "equals", "value": "right"})])
    assert rows[0]["ok"] is False
    assert "expected 'right'" in rows[0]["reason"]


def test_client_exception_becomes_a_failed_task_not_a_crash():
    class Boom:
        def call(self, name, args=None):
            raise RuntimeError("provider hang")

    rows = runner.run_eval(Boom(), SAMPLE_TASKS)
    assert rows[0]["ok"] is False
    assert "RuntimeError" in rows[0]["reason"]


# --------------------------------------------------------------------------
# tasks.json contract
# --------------------------------------------------------------------------

def test_golden_set_loads_and_is_sane():
    tasks = runner.load_tasks(EVAL_DIR / "tasks.json")
    assert len(tasks) == 12
    assert {t["app"] for t in tasks} == {"notepad", "calc", "explorer", "settings", "chrome-dom"}
    for task in tasks:
        assert task["goal"] and task["steps_hint"]
        assert task["expect"]["check"] in runner.CHECKS


def test_golden_set_rejects_bad_files(tmp_path):
    bad = [
        {"tasks": [{"id": "t", "app": "x", "goal": "g", "steps_hint": [], "expect": {"tool": "f"}}]},  # no check
        {"tasks": [{"id": "t", "app": "x", "goal": "g", "steps_hint": [], "expect": {"tool": "f", "check": "nope"}}]},
        {"tasks": [{"id": "dup", "app": "x", "goal": "g", "steps_hint": [], "expect": {"tool": "f", "check": "ok"}},
                   {"id": "dup", "app": "x", "goal": "g", "steps_hint": [], "expect": {"tool": "f", "check": "ok"}}]},
        {"tasks": [{"id": "t", "app": "x", "goal": "g", "steps_hint": [], "expect": {"tool": "f", "check": "matches", "value": "v"}}]},
        {"tasks": [{"id": "t", "app": "x", "goal": "g", "steps_hint": [], "expect": {"tool": "f", "check": "matches", "regex": "(", "value": "v"}}]},
    ]
    for payload in bad:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(TaskFormatError):
            runner.load_tasks(path)


def test_full_golden_set_passes_against_smoke_client():
    tasks = runner.load_tasks(EVAL_DIR / "tasks.json")
    rows = runner.run_eval(runner.smoke_client(tasks), tasks)
    assert all(r["ok"] for r in rows), [(r["id"], r["reason"]) for r in rows if not r["ok"]]
    assert runner.success_rate(rows) == 1.0


# --------------------------------------------------------------------------
# reporting + CLI
# --------------------------------------------------------------------------

def test_markdown_table_and_results_json(tmp_path):
    rows = runner.run_eval(sample_client(), SAMPLE_TASKS)
    table = runner.format_table(rows)
    assert table.splitlines()[0].startswith("| # | task | app | ok |")
    assert "| PASS |" in table
    assert "1.00" in table

    out = tmp_path / "nested" / "results.json"
    payload = runner.write_results(rows, out)
    assert payload["passed"] == 3 and payload["total"] == 3
    assert json.loads(out.read_text(encoding="utf-8"))["success_rate"] == 1.0


def test_cli_runs_end_to_end(tmp_path, capsys):
    out = tmp_path / "results.json"
    rc = runner.main(["--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["total"] == 12 and payload["passed"] == 12
    printed = capsys.readouterr().out
    assert "| PASS |" in printed and "success rate 1.00" in printed


def test_cli_gate_fails_below_threshold(tmp_path):
    code = runner.main(["--out", str(tmp_path / "r.json"), "--min-success", "0.9"])
    assert code == 0  # smoke client is green at 12/12
    code = runner.main(["--tasks", str(_failing_tasks(tmp_path)), "--out", str(tmp_path / "r2.json"),
                        "--min-success", "0.8"])
    assert code == 1


def test_cli_rejects_bad_threshold():
    assert runner.main(["--min-success", "5"]) == 2
    assert runner.main(["--min-success", "-1"]) == 2


def _failing_tasks(tmp_path) -> str:
    """A task file whose expectation the fake cannot satisfy.

    Uses a `contains` check on purpose: the `equals` path derives its
    answer from the task itself (that is what the smoke client is), so a
    negative gate test needs an expectation nothing in the canned data
    contains.
    """
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": [
        make_task("doomed", {"tool": "list_windows", "check": "contains", "value": "Zebra Explorer"}),
        make_task("doomed-2", {"tool": "get_value", "check": "equals", "value": "hello from win-mcp-pro"}),
    ]}), encoding="utf-8")
    return str(path)


def test_cli_help_works(capsys):
    with pytest.raises(SystemExit) as exc:
        runner.main(["--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()
