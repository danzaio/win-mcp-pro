"""stdio MCP server entrypoint.

Hard rule of the stdio transport: stdout carries the JSON-RPC framing, so
this module never prints to it. Every log line, the audit trail included,
goes to stderr (see ``server.audit``).

Run with::

    python -m server.main [--tools find,invoke] [--exclude-tools kill_proc]
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import registry
from .audit import audit

__all__ = ["build_server", "run_stdio_server", "main"]


def _call_handler(handler: Any, arguments: dict) -> Any:
    """Invoke a tool handler regardless of its calling convention.

    Canonical handlers take ``**arguments``. The winproc/vision/compat
    slices take a single positional dict instead. kwargs-first preserves
    the 31 working tools; the fallback fixes the other 22 without
    touching them.
    """
    try:
        return handler(**arguments)
    except TypeError as first_exc:
        try:
            sig = inspect.signature(handler)
        except (ValueError, TypeError):
            raise first_exc
        params = list(sig.parameters.values())
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            raise first_exc
        positional = [
            p
            for p in params
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        required = [p for p in positional if p.default is inspect.Parameter.empty]
        if len(params) == 1 or (len(required) == 1 and positional and params[0] is positional[0]):
            return handler(arguments)
        raise first_exc


def _bridge_tools_to_registry() -> int:
    """Production wiring: discover tool modules and publish to the server registry.

    ``tools.load_all_modules()`` fills ``tools.TOOL_REGISTRY``; ``build_server``
    reads ``server.registry``. Without this bridge the server lists 0 tools.
    Idempotent: clears both sides first so a second boot does not clash.
    """
    import tools

    tools.clear()
    registry.reset_registry()
    tools.load_all_modules()
    registry.register_module(tools.TOOL_REGISTRY)
    return len(registry.registered_tools())


def _tool_schema(tdef: registry.ToolDef) -> dict:
    """JSON Schema for a tool: pydantic model class or raw dict."""
    schema = tdef.schema
    if isinstance(schema, type) and hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return schema
    raise TypeError(
        f"tool {tdef.name!r} schema must be a pydantic model class or a dict, "
        f"got {schema!r}"
    )


def build_server(
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> Server:
    """Build the MCP server exposing the registered, policy-filtered tools.

    The registry is read per request, so tools registered after this call
    still show up in list_tools / call_tool.
    """

    async def on_list_tools(ctx, params):
        tools = registry.visible_tools(allowlist, blocklist)
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t.name,
                    description=t.description,
                    input_schema=_tool_schema(t),
                )
                for t in tools
            ]
        )

    async def on_call_tool(ctx, params):
        name = params.name
        arguments = dict(params.arguments or {})
        audit({"event": "tool_call", "tool": name, "arguments": sorted(arguments)})
        tdefs = {t.name: t for t in registry.visible_tools(allowlist, blocklist)}
        tdef = tdefs.get(name)
        if tdef is None:
            audit({"event": "tool_error", "tool": name, "error": "UNKNOWN_TOOL"})
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps({"error": "UNKNOWN_TOOL", "tool": name}),
                    )
                ],
                is_error=True,
            )
        try:
            result = _call_handler(tdef.handler, arguments)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            # a blowing-up tool must not take the server down with it
            audit(
                {"event": "tool_error", "tool": name, "error": type(exc).__name__}
            )
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": type(exc).__name__, "message": str(exc)}
                        ),
                    )
                ],
                is_error=True,
            )
        audit({"event": "tool_ok", "tool": name})
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], is_error=False
        )

    return Server("win-mcp-pro", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _serve(server: Server) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


def run_stdio_server(
    allowlist: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> None:
    """Serve MCP on stdio until the client closes the connection."""
    count = _bridge_tools_to_registry()
    # build_server() alone stays lazy so in-memory test sessions control
    # exactly which tools exist; production always bridges first.
    server = build_server(allowlist, blocklist)
    audit(
        {
            "event": "server_start",
            "allowlist": allowlist,
            "blocklist": blocklist,
            "tools": count,
        }
    )
    asyncio.run(_serve(server))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="win-mcp-pro",
        description="Windows computer-use MCP server (stdio; logs to stderr only).",
    )
    parser.add_argument(
        "--tools", help="comma-separated allowlist of tool names to expose"
    )
    parser.add_argument(
        "--exclude-tools", help="comma-separated blocklist of tool names to hide"
    )
    args = parser.parse_args(argv)
    # Fail-closed filtering: an explicit but empty --tools is a config error
    # (never silently "allow everything"); absent flags mean unrestricted.
    from .security import parse_tool_filter

    filter_argv: list[str] = []
    if args.tools is not None:
        filter_argv += ["--tools", args.tools]
    if args.exclude_tools is not None:
        filter_argv += ["--exclude-tools", args.exclude_tools]
    try:
        allowlist, blocklist = parse_tool_filter(filter_argv)
    except ValueError as exc:
        parser.error(str(exc))
    registry.run_server(
        allowlist=sorted(allowlist) if allowlist is not None else None,
        blocklist=sorted(blocklist) if blocklist is not None else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
