"""Audit trail: one JSON object per event, appended to stderr.

The stdio MCP transport owns stdout (JSON-RPC frames), so nothing except
protocol bytes may ever be written there. Audit lines are JSONL on stderr:
one event, one line.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

__all__ = ["audit"]


def audit(event: dict) -> None:
    """Append ``event`` to stderr as a single JSON line.

    A timestamp is added under ``ts``. This never raises: a broken event
    must not take the server down, and even the fallback line records that
    something happened.
    """
    record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    try:
        line = json.dumps(record, default=str)
    except (TypeError, ValueError):
        line = json.dumps(
            {"ts": record["ts"], "event": "audit_serialization_failed"}, default=str
        )
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        # stderr closed or unwritable: lose the line, not the server
        pass
