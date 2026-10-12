"""Tool registry.

Tool modules expose ``TOOLS`` either as attribute-style objects (the Task 1
contract: ``.name``, ``.description``, ``.schema`` as a pydantic BaseModel
subclass, ``.handler`` callable) or as plain dicts with those four keys.
``register_module`` normalizes both shapes into ``ToolDef`` so the rest of
the server never cares which style a module used.

``run_server(allowlist, blocklist)`` starts the stdio MCP server with the
``--tools`` / ``--exclude-tools`` policy applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .audit import audit

__all__ = [
    "ToolDef",
    "register_module",
    "registered_tools",
    "visible_tools",
    "reset_registry",
    "run_server",
]


@dataclass(frozen=True)
class ToolDef:
    """Normalized tool definition (attribute access, always)."""

    name: str
    description: str
    schema: Any
    handler: Callable[..., Any]


_REQUIRED = ("name", "description", "schema", "handler")

_TOOLS: dict[str, ToolDef] = {}


def _normalize(tool: Any) -> ToolDef:
    """Accept an attribute-style def or a dict; return a ToolDef."""
    if isinstance(tool, dict):
        missing = [key for key in _REQUIRED if key not in tool]
        if missing:
            raise ValueError(f"tool definition missing keys {missing}: {tool!r}")
        values = {key: tool[key] for key in _REQUIRED}
    else:
        missing = [key for key in _REQUIRED if not hasattr(tool, key)]
        if missing:
            raise ValueError(f"tool definition missing attributes {missing}: {tool!r}")
        values = {key: getattr(tool, key) for key in _REQUIRED}

    name = values["name"]
    if not isinstance(name, str) or not name:
        raise ValueError(f"tool name must be a non-empty string, got {name!r}")
    if not callable(values["handler"]):
        raise ValueError(f"tool {name!r} handler is not callable: {values['handler']!r}")
    return ToolDef(**values)


def register_module(TOOLS: Iterable[Any]) -> None:
    """Register one module's ``TOOLS`` list (either style)."""
    for tool in TOOLS:
        tdef = _normalize(tool)
        if tdef.name in _TOOLS:
            raise ValueError(f"duplicate tool name: {tdef.name}")
        _TOOLS[tdef.name] = tdef
        audit({"event": "register", "tool": tdef.name})


def registered_tools() -> list[ToolDef]:
    """Every registered tool, in registration order."""
    return list(_TOOLS.values())


def visible_tools(
    allowlist: Iterable[str] | None = None,
    blocklist: Iterable[str] | None = None,
) -> list[ToolDef]:
    """Registered tools after the allowlist/blocklist policy.

    ``allowlist`` keeps only the named tools; ``blocklist`` drops the named
    ones. ``None`` means "no filter" for that axis. An empty list is a
    filter too (allow nothing / drop nothing), which is what the CLI flags
    produce for an empty value.
    """
    tools = registered_tools()
    if allowlist is not None:
        keep = set(allowlist)
        tools = [t for t in tools if t.name in keep]
    if blocklist is not None:
        drop = set(blocklist)
        tools = [t for t in tools if t.name not in drop]
    return tools


def reset_registry() -> None:
    """Drop every registration. Test helper; the server never calls this."""
    _TOOLS.clear()


def run_server(
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
    *,
    uia_worker: bool = False,
    worker_timeout_ms: int = 15_000,
    worker_command: str | None = None,
) -> None:
    """Start the stdio MCP server over the registered tools."""
    from .main import run_stdio_server  # local import: main pulls in the mcp SDK

    run_stdio_server(
        allowlist=allowlist,
        blocklist=blocklist,
        uia_worker=uia_worker,
        worker_timeout_ms=worker_timeout_ms,
        worker_command=worker_command,
    )
