"""UIA worker process (Phase B3): executes the walk tools behind a pipe.

Run as ``python -m server.worker``; the supervisor (server/supervisor.py)
spawns it lazily and owns its lifecycle. Protocol: one JSON request per
line on stdin (``{"id": n, "tool": name, "args": {...}}``), one JSON reply
per line on stdout (``{"id": n, "result": {...}}``). stdout carries nothing
else — tool output goes through the reply, logs stay on stderr. The
process exits on stdin EOF, so a dead supervisor never leaves an orphan.

Only the walk tools are served here (the supervisor refuses the rest); a
crashing or hanging handler is contained by the parent's deadline and kill.
"""
from __future__ import annotations

import json
import sys


def _dispatch(name: str, args: dict) -> object:
    import tools

    if not tools.TOOL_REGISTRY:
        tools.load_all_modules()
    entry = tools.get(name)
    if entry is None:
        return {"ok": False, "error": "UNKNOWN_TOOL", "reason": str(name)}
    try:
        reply = tools.call_handler(entry["handler"], dict(args or {}))
    except Exception as exc:  # a blown-up walk is data for the caller
        return {"ok": False, "error": type(exc).__name__, "reason": str(exc)[:300]}
    dump = getattr(reply, "model_dump", None)
    if callable(dump):  # pydantic results (snapshot) cross the pipe as plain JSON
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    return reply


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = _dispatch(str(message.get("tool")), message.get("args") or {})
        sys.stdout.write(
            json.dumps({"id": message.get("id"), "result": reply}, default=str) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
