"""Tests for tools.replay - recorded flows with assertions and drift refusal.

The engine runs registered tool handlers in-process; these tests install
fake tools into ``tools.TOOL_REGISTRY`` so the flow semantics (id binding,
drift, ambiguity) are pinned without touching UIA.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import win_mcp_pro.tools as tools
from win_mcp_pro.tools import replay


@pytest.fixture()
def fake_tools():
    saved = list(tools.TOOL_REGISTRY)
    tools.TOOL_REGISTRY.clear()

    def add(name, handler):
        tools.TOOL_REGISTRY.append(
            {"name": name, "description": "fake", "schema": object, "handler": handler}
        )

    def fake_find(args):
        return {"ok": True, "element": {"id": "W:42,7", "name": args.get("name", "")}}

    def fake_find_no_id(args):
        return {"ok": True, "note": "nothing to bind"}

    def fake_get_prop(args):
        return {"ok": True, "id": args.get("id"), "value": "the-value"}

    def fake_fail(args):
        return {"ok": False, "error": "NOPE"}

    def fake_boom(args):
        raise RuntimeError("kaboom")

    add("fake_find", fake_find)
    add("fake_find_no_id", fake_find_no_id)
    add("fake_get_prop", fake_get_prop)
    add("fake_fail", fake_fail)
    add("fake_boom", fake_boom)
    try:
        yield
    finally:
        tools.TOOL_REGISTRY[:] = saved


def test_replay_binds_ids_from_earlier_steps(fake_tools):
    result = replay.handler_replay({
        "steps": [
            {"tool": "fake_find", "args": {"name": "Save"},
             "expect": {"ok": True}},
            {"tool": "fake_get_prop", "args": {"id": "@id:0"},
             "expect": {"value": "the-value"}},
        ],
    })
    assert result["ok"] is True, result
    assert result["steps"] == 2
    assert result["records"][1]["result"]["id"] == "W:42,7"


def test_replay_reports_drift_with_step_and_values(fake_tools):
    result = replay.handler_replay({
        "steps": [
            {"tool": "fake_find", "args": {}, "expect": {"ok": True}},
            {"tool": "fake_get_prop", "args": {"id": "@id:0"},
             "expect": {"value": "EXPECTED-OTHER"}},
        ],
    })
    assert result["ok"] is False
    assert result["error"] == "DRIFT"
    assert result["step"] == 1
    mismatch = result["mismatches"][0]
    assert mismatch["key"] == "value"
    assert mismatch["expected"] == "EXPECTED-OTHER"
    assert mismatch["actual"] == "the-value"


def test_replay_refuses_ambiguous_id_binding(fake_tools):
    result = replay.handler_replay({
        "steps": [
            {"tool": "fake_find_no_id", "args": {}, "expect": {"ok": True}},
            {"tool": "fake_get_prop", "args": {"id": "@id:0"}, "expect": {}},
        ],
    })
    assert result["ok"] is False
    assert result["error"] == "AMBIGUOUS_ID"
    assert result["step"] == 1


def test_replay_unknown_tool_is_typed(fake_tools):
    result = replay.handler_replay({"steps": [{"tool": "no_such_tool"}]})
    assert result["error"] == "UNKNOWN_TOOL"
    assert result["step"] == 0


def test_replay_failed_step_without_expectations_is_drift(fake_tools):
    result = replay.handler_replay({"steps": [{"tool": "fake_fail", "args": {}}]})
    assert result["ok"] is False
    assert result["error"] == "DRIFT"
    assert result["mismatches"][0]["key"] == "ok"


def test_replay_explicit_failure_expectation_passes(fake_tools):
    result = replay.handler_replay({
        "steps": [{"tool": "fake_fail", "args": {}, "expect": {"ok": False, "error": "NOPE"}}],
    })
    assert result["ok"] is True, result


def test_replay_handler_exception_is_step_failed(fake_tools):
    result = replay.handler_replay({"steps": [{"tool": "fake_boom", "args": {}}]})
    assert result["ok"] is False
    assert result["error"] == "STEP_FAILED"
    assert "kaboom" in result["reason"]


def test_replay_schema_requires_at_least_one_step():
    with pytest.raises(ValidationError):
        replay.ReplayArgs(steps=[])


def test_replay_tool_surface():
    names = {t["name"] for t in replay.TOOLS}
    assert names == {"replay"}
    for tool in replay.TOOLS:
        assert tool["description"].strip()
        assert callable(tool["handler"])

def test_replay_runs_keyword_only_handlers(fake_tools):
    """Real tools such as set_value/invoke/snapshot take keywords, not one dict."""
    seen = []

    def keyword_only(id, value=""):
        seen.append((id, value))
        return {"ok": True, "id": id}

    tools.TOOL_REGISTRY.append(
        {"name": "fake_keyword", "description": "fake", "schema": object, "handler": keyword_only}
    )
    result = replay.handler_replay({
        "steps": [
            {"tool": "fake_find", "args": {}, "expect": {"ok": True}},
            {"tool": "fake_keyword", "args": {"id": "@id:0", "value": "x"}, "expect": {"ok": True}},
        ],
    })
    assert result["ok"] is True, result
    assert seen == [("W:42,7", "x")]
