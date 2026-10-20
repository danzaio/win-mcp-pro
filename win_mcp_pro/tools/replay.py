"""Replay (Phase E): re-run a recorded tool flow with assertions.

A replay is a list of steps; each step names a registered tool, its
arguments and optional expectations (top-level key equality on the tool's
JSON result). Action steps bind to ids the flow itself produced: a string
argument of the exact form ``"@id:<step>"`` is replaced by the element id
recorded from that step (``element.id`` or ``id``). If the referenced step
produced no id, the replay refuses with ``AMBIGUOUS_ID`` instead of
guessing - re-finding by name could silently hit a different control.

A failed expectation is ``DRIFT``: the replay stops, and the response names
the step, the key and both values. A step whose tool answered ``ok: false``
without explicit expectations is also drift (failures never pass silently).

The replay runs the same handlers the MCP server exposes, in-process; it
inherits the caller's authority (no extra sandbox) and read-only flows are
the intended use.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Exact placeholder for "the element id produced by step N".
_ID_PLACEHOLDER = re.compile(r"^@id:(\d+)$")
_MISSING = object()


class ReplayStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, description="Registered tool name.")
    args: dict[str, Any] = Field(default_factory=dict,
                                 description="Arguments; '@id:N' binds step N's element id.")
    expect: dict[str, Any] = Field(
        default_factory=dict,
        description="Top-level result keys that must equal these values.",
    )
    label: str | None = Field(default=None, description="Optional human label.")


class ReplayArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[ReplayStep] = Field(min_length=1, max_length=50)
    stop_on_drift: bool = Field(
        default=True, description="Stop at the first drift (default) instead of continuing."
    )


class _Ambiguous(Exception):
    pass


def _resolve_placeholders(value: Any, ids: list[str | None]) -> Any:
    if isinstance(value, str):
        match = _ID_PLACEHOLDER.match(value)
        if match is None:
            return value
        index = int(match.group(1))
        if index >= len(ids) or ids[index] is None:
            raise _Ambiguous(
                "@id:%d refers to a step that produced no element id" % index
            )
        return ids[index]
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item, ids) for item in value]
    return value


def _extract_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    element = result.get("element")
    if isinstance(element, dict) and isinstance(element.get("id"), str):
        return element["id"]
    if isinstance(result.get("id"), str):
        return result["id"]
    return None


def handler_replay(args: Any) -> dict:
    import win_mcp_pro.tools as tools_pkg

    m = ReplayArgs(**dict(args or {}))
    ids: list[str | None] = []
    records: list[dict] = []
    for index, step in enumerate(m.steps):
        entry = tools_pkg.get(step.tool)
        if entry is None:
            return {"ok": False, "error": "UNKNOWN_TOOL", "step": index,
                    "tool": step.tool, "records": records}
        try:
            call_args = _resolve_placeholders(dict(step.args), ids)
        except _Ambiguous as exc:
            return {"ok": False, "error": "AMBIGUOUS_ID", "step": index,
                    "tool": step.tool, "reason": str(exc), "records": records}
        try:
            result = tools_pkg.call_handler(entry["handler"], call_args)
        except Exception as exc:  # noqa: BLE001 - a blowing-up step is data
            return {"ok": False, "error": "STEP_FAILED", "step": index,
                    "tool": step.tool,
                    "reason": "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                    "records": records}
        ids.append(_extract_id(result))

        mismatches: list[dict] = []
        if step.expect:
            for key, expected in step.expect.items():
                actual = result.get(key, _MISSING) if isinstance(result, dict) else _MISSING
                if actual is _MISSING or actual != expected:
                    mismatches.append({
                        "key": key,
                        "expected": expected,
                        "actual": actual if actual is not _MISSING else "<missing>",
                    })
        elif isinstance(result, dict) and result.get("ok") is False:
            mismatches.append({"key": "ok", "expected": True, "actual": False})

        records.append({"step": index, "tool": step.tool, "label": step.label,
                        "result": result, "mismatches": mismatches})
        if mismatches and m.stop_on_drift:
            return {"ok": False, "error": "DRIFT", "step": index,
                    "tool": step.tool, "mismatches": mismatches, "records": records}
    return {"ok": True, "steps": len(m.steps), "records": records}


def _tool(name: str, description: str, schema, handler) -> dict:
    return {"name": name, "description": description, "schema": schema,
            "handler": handler}


TOOLS = [
    _tool("replay",
          "Re-run a recorded tool flow step by step with assertions: each step "
          "names a tool, its args (a string arg '@id:N' binds the element id "
          "from step N - an unresolvable binding refuses as AMBIGUOUS_ID) and "
          "optional expected top-level result values. The first mismatch stops "
          "the replay with DRIFT and the exact key/expected/actual; a step "
          "that answered ok=false without expectations is drift too.",
          ReplayArgs, handler_replay),
]
