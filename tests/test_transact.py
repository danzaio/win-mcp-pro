"""Tests for tools.transact — transactions + audit (Task 11).

Covers the four MCP tools (preview, dry_run, before_after, profile) plus the
idempotency registry other tool modules call through ``check_and_mark``.

Everything runs against in-memory fakes: no UIA, no real execution.
"""

import pytest
from pydantic import ValidationError

from tools import transact


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    """transact keeps process-wide state; isolate every case."""
    transact.reset()
    yield
    transact.reset()


class FakeExecutor:
    """Records calls and returns a canned ActionResult."""

    def __init__(self, result=None, boom=None):
        self.calls = []
        self.result = result or {
            "ok": True,
            "before": {"value": "old"},
            "after": {"value": "new"},
        }
        self.boom = boom

    def __call__(self, action):
        self.calls.append(action)
        if self.boom is not None:
            raise self.boom
        return dict(self.result)


def make_action(**kw):
    base = {"window": "Notepad", "element": "0x1F2A3B:btn-send", "action": "invoke"}
    base.update(kw)
    return transact.ActionIn(**base)


@pytest.fixture
def executor():
    return FakeExecutor()


@pytest.fixture
def clock(monkeypatch):
    """Deterministic monotonic clock for window-expiry tests."""
    state = {"t": 1000.0}
    monkeypatch.setattr(transact, "_monotonic", lambda: state["t"])
    return state


def tool(name):
    for defn in transact.TOOLS:
        if defn["name"] == name:
            return defn
    raise AssertionError(f"tool {name!r} not in TOOLS")


# --- schema -------------------------------------------------------------------


def test_action_requires_non_empty_target():
    with pytest.raises(ValidationError):
        transact.ActionIn(window="", element="edit", action="invoke")
    with pytest.raises(ValidationError):
        transact.ActionIn(window="Notepad", element="edit", action="")


def test_args_hash_is_order_independent():
    a = transact.args_hash({"text": "hi", "n": 1})
    b = transact.args_hash({"n": 1, "text": "hi"})
    assert a == b
    assert a != transact.args_hash({"text": "ho"})


# --- preview ------------------------------------------------------------------


def test_preview_describes_target_and_effect_without_executing(executor):
    transact.register_executor("set_value", executor)
    result = transact.preview(
        make_action(action="set_value", args={"text": "hello"})
    )
    assert result["ok"] is True
    assert result["preview_id"].startswith("pv_")
    assert result["target"] == {"window": "Notepad", "element": "0x1F2A3B:btn-send"}
    assert "0x1F2A3B:btn-send" in result["effect"]
    assert "set_value" in result["effect"]
    assert executor.calls == []


def test_preview_flags_irreversibility():
    send = transact.preview(make_action(action="send"))
    assert send["irreversible"] is True
    assert send["reversible"] is False
    assert send["confirm_required"] is True

    invoke = transact.preview(make_action(action="invoke"))
    assert invoke["irreversible"] is False
    assert invoke["reversible"] is True
    assert invoke["confirm_required"] is False


def test_args_can_declare_irreversibility():
    result = transact.preview(make_action(action="weird_op", args={"irreversible": True}))
    assert result["irreversible"] is True


def test_preview_id_is_single_use(executor):
    action = make_action(action="submit")
    preview = transact.preview(action)
    first = transact.execute_action(
        action.model_copy(update={"preview_id": preview["preview_id"]}), executor
    )
    assert first["ok"] is True
    gate = transact.preview_gate(action)
    assert gate is not None and gate["error"] == "PREVIEW_REQUIRED"


def test_preview_does_not_cover_different_args(executor):
    preview = transact.preview(make_action(action="delete", args={"target": "old.txt"}))
    result = transact.execute_action(
        make_action(
            action="delete",
            args={"target": "keep.txt"},
            preview_id=preview["preview_id"],
        ),
        executor,
    )
    assert result["ok"] is False
    assert result["error"] == "PREVIEW_REQUIRED"
    assert executor.calls == []


def test_preview_expires(executor, clock):
    action = make_action(action="send")
    preview = transact.preview(action)
    clock["t"] += transact.PREVIEW_TTL + 1
    result = transact.execute_action(
        action.model_copy(update={"preview_id": preview["preview_id"]}), executor
    )
    assert result["ok"] is False
    assert result["error"] == "PREVIEW_REQUIRED"
    assert executor.calls == []


# --- idempotency ----------------------------------------------------------------


def test_duplicate_action_executes_once(executor):
    action = make_action()
    first = transact.execute_action(action, executor)
    second = transact.execute_action(action, executor)
    assert first["ok"] is True
    assert first.get("deduped") is not True
    assert second["ok"] is True
    assert second["deduped"] is True
    assert len(executor.calls) == 1
    executed = [e for e in transact.recent() if not e["deduped"]]
    assert len(executed) == 1


def test_duplicate_different_args_still_executes(executor):
    transact.execute_action(make_action(args={"text": "a"}), executor)
    result = transact.execute_action(make_action(args={"text": "b"}), executor)
    assert result.get("deduped") is not True
    assert len(executor.calls) == 2


def test_idempotency_window_expires(executor, clock):
    action = make_action()
    transact.execute_action(action, executor)
    clock["t"] += transact.DEDUPE_WINDOW_S + 1
    result = transact.execute_action(action, executor)
    assert result.get("deduped") is not True
    assert len(executor.calls) == 2


def test_check_and_mark_helper_accepts_action_or_key():
    action = make_action()
    first = transact.check_and_mark(action)
    assert first["deduped"] is False
    second = transact.check_and_mark(transact.action_key(action))
    assert second["deduped"] is True
    assert second["age_s"] >= 0


# --- irreversible gate -----------------------------------------------------------


def test_irreversible_without_preview_is_blocked(executor):
    result = transact.execute_action(make_action(action="send"), executor)
    assert result["ok"] is False
    assert result["error"] == "PREVIEW_REQUIRED"
    assert "preview" in result["reason"]
    assert executor.calls == []


def test_irreversible_with_preview_executes(executor):
    action = make_action(action="send")
    preview = transact.preview(action)
    result = transact.execute_action(
        action.model_copy(update={"preview_id": preview["preview_id"]}), executor
    )
    assert result["ok"] is True
    assert len(executor.calls) == 1


def test_reversible_action_needs_no_preview(executor):
    result = transact.execute_action(make_action(action="invoke"), executor)
    assert result["ok"] is True
    assert len(executor.calls) == 1


# --- before_after ring log --------------------------------------------------------


def test_before_after_returns_recorded_entry(executor):
    result = transact.execute_action(make_action(), executor)
    found = transact.before_after(result["action_id"])
    assert found["ok"] is True
    entry = found["entry"]
    assert entry["before"] == {"value": "old"}
    assert entry["after"] == {"value": "new"}
    assert entry["action"] == "invoke"
    assert entry["ok"] is True
    assert entry["deduped"] is False


def test_before_after_unknown_id():
    result = transact.before_after("act_missing")
    assert result["ok"] is False
    assert result["error"] == "NOT_FOUND"


def test_ring_keeps_last_50(executor):
    ids = []
    for i in range(60):
        result = transact.execute_action(make_action(args={"i": i}), executor)
        ids.append(result["action_id"])
    assert len(transact.recent()) == transact.RING_SIZE
    for old_id in ids[:10]:
        assert transact.before_after(old_id)["error"] == "NOT_FOUND"
    for kept_id in ids[10:]:
        assert transact.before_after(kept_id)["ok"] is True
    assert len(executor.calls) == 60


# --- dry_run ----------------------------------------------------------------------


def test_dry_run_does_not_execute_or_mark(executor):
    action = make_action()
    for _ in range(2):
        result = transact.dry_run(action)
        assert result["ok"] is True
        assert result["performed"] is False
        assert result["would_execute"] is True
        assert result["blocked_by"] is None
        assert executor.calls == []
    assert transact.check_and_mark(action)["deduped"] is False


def test_dry_run_reports_preview_requirement():
    result = transact.dry_run(make_action(action="submit"))
    assert result["ok"] is True
    assert result["performed"] is False
    assert result["would_execute"] is False
    assert result["blocked_by"] == "PREVIEW_REQUIRED"
    assert result["preview_required"] is True


def test_dry_run_reports_dedupe(executor):
    action = make_action()
    transact.execute_action(action, executor)
    result = transact.dry_run(action)
    assert result["blocked_by"] == "DEDUPE"
    assert result["deduped"] is True
    assert result["would_execute"] is False


# --- profile ------------------------------------------------------------------------


def test_profile_measures_stages_and_tokens_without_executing(executor):
    transact.register_executor("invoke", executor)
    result = transact.profile(make_action())
    assert result["ok"] is True
    assert result["executed"] is False
    assert set(result["stages_ms"]) == {"preview", "guard", "dedupe", "execute"}
    assert all(ms >= 0 for ms in result["stages_ms"].values())
    assert result["tokens_saved_est"] > 0
    assert result["tokens"]["saved_est"] == result["tokens_saved_est"]
    assert result["tokens"]["saved_est"] == (
        transact.VISION_BASELINE_TOKENS - transact.UIA_TOKENS_PER_ACTION
    )
    assert executor.calls == []


def test_profile_can_execute_with_registered_executor(executor):
    transact.register_executor("invoke", executor)
    result = transact.profile(transact.ProfileIn(
        window="Notepad", element="0x1F2A3B:btn-send", action="invoke", execute=True
    ))
    assert result["ok"] is True
    assert result["executed"] is True
    assert len(executor.calls) == 1
    assert result["stages_ms"]["execute"] >= 0


def test_profile_blocked_action_does_not_execute(executor):
    transact.register_executor("send", executor)
    result = transact.profile(transact.ProfileIn(
        window="Notepad", element="0x1F2A3B:btn-send", action="send", execute=True
    ))
    assert result["ok"] is True
    assert result["executed"] is False
    assert result["blocked_by"] == "PREVIEW_REQUIRED"
    assert executor.calls == []


def test_profile_unknown_executor_is_typed_error():
    result = transact.profile(transact.ProfileIn(
        window="Notepad", element="0x1F2A3B:btn-send", action="nope", execute=True
    ))
    assert result["ok"] is False
    assert result["error"] == "UNKNOWN_ACTION"


# --- executor failure ---------------------------------------------------------------


def test_executor_failure_is_typed_and_recorded():
    boom = RuntimeError("COM exploded")
    result = transact.execute_action(make_action(), FakeExecutor(boom=boom))
    assert result["ok"] is False
    assert result["error"] == "EXECUTOR_ERROR"
    assert "COM exploded" in result["reason"]
    entry = transact.before_after(result["action_id"])["entry"]
    assert entry["ok"] is False
    assert entry["error"] == "EXECUTOR_ERROR"


# --- registry contract -----------------------------------------------------------------


def test_tools_expose_four_tools_with_pydantic_schemas():
    assert {d["name"] for d in transact.TOOLS} == {
        "preview", "dry_run", "before_after", "profile",
    }
    for defn in transact.TOOLS:
        assert defn["description"].strip()
        assert hasattr(defn["schema"], "model_json_schema")
        assert callable(defn["handler"])
        assert "screenshot" not in defn["name"]
    props = tool("preview")["schema"].model_json_schema()["properties"]
    assert set(props) >= {"window", "element", "action", "args", "preview_id"}


def test_handlers_accept_kwargs_like_the_server(executor):
    transact.register_executor("invoke", executor)
    result = tool("preview")["handler"](
        window="Notepad", element="0x1F2A3B:btn-send", action="invoke"
    )
    assert result["ok"] is True
    bad = tool("preview")["handler"](window="Notepad")
    assert bad["ok"] is False
    assert bad["error"] == "BAD_REQUEST"


def test_handler_before_after(executor):
    result = transact.execute_action(make_action(), executor)
    found = tool("before_after")["handler"](action_id=result["action_id"])
    assert found["ok"] is True
