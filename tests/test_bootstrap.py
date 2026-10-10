"""Task 2: MCP stdio bootstrap.

Two behaviors matter at this layer:
- list_tools returns the registered tool names. Tool modules expose TOOLS
  either as attribute-style objects (.name/.description/.schema/.handler,
  the Task 1 contract) or as plain dicts; the registry normalizes both.
- the audit trail is JSONL on stderr, never on stdout (stdout is the MCP
  transport channel).

The suite ends with a real in-memory MCP session (initialize → list_tools
→ call_tool) so the bootstrap is proven against the actual SDK without
touching the stdio transport.
"""

import asyncio
import json

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pydantic import BaseModel

from server import audit, main, registry


# --- fake tool modules ---------------------------------------------------------


class FakeArgs(BaseModel):
    id: str


def _handler(**kwargs):
    return {"ok": True}


class AttrToolDef:
    """Attribute-style ToolDef: the shape tools/* modules expose."""

    def __init__(self, name, description="fake tool", schema=FakeArgs, handler=_handler):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler


def obj_module():
    return [AttrToolDef("find"), AttrToolDef("invoke")]


def dict_module():
    return [
        {"name": "find", "description": "fake tool", "schema": FakeArgs, "handler": _handler},
        {"name": "invoke", "description": "fake tool", "schema": FakeArgs, "handler": _handler},
    ]


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset_registry()
    yield
    registry.reset_registry()


@pytest.fixture
def obj_registered():
    registry.register_module(obj_module())


# --- list_tools -----------------------------------------------------------------


def test_list_tools_returns_registered_names(obj_registered):
    names = [t.name for t in registry.visible_tools()]
    assert names == ["find", "invoke"]


def test_registry_accepts_dict_style_defs():
    registry.register_module(dict_module())
    tools = registry.visible_tools()
    assert [t.name for t in tools] == ["find", "invoke"]
    for t in tools:  # normalized to attribute access
        assert t.description
        assert t.handler is _handler
        assert issubclass(t.schema, BaseModel)


def test_allowlist_limits_visible_tools(obj_registered):
    assert [t.name for t in registry.visible_tools(allowlist=["find"])] == ["find"]


def test_blocklist_hides_tool(obj_registered):
    assert [t.name for t in registry.visible_tools(blocklist=["invoke"])] == ["find"]


def test_register_module_rejects_malformed_def():
    with pytest.raises(ValueError):
        # dict missing the handler key
        registry.register_module([{"name": "broken", "description": "d", "schema": FakeArgs}])


def test_register_module_rejects_non_callable_handler():
    with pytest.raises(ValueError):
        registry.register_module([AttrToolDef("broken", handler="not-callable")])


def test_duplicate_tool_name_rejected(obj_registered):
    with pytest.raises(ValueError):
        registry.register_module([AttrToolDef("find")])


# --- audit -----------------------------------------------------------------------


def test_audit_writes_to_stderr_not_stdout(capsys):
    audit.audit({"event": "tool_call", "tool": "find", "id": "1234:5678"})
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is the MCP transport: nothing may land there
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["event"] == "tool_call"
    assert record["tool"] == "find"
    assert record["id"] == "1234:5678"


def test_audit_is_jsonl_one_line_per_event(capsys):
    audit.audit({"event": "a"})
    audit.audit({"event": "b"})
    lines = capsys.readouterr().err.strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["event"] for line in lines] == ["a", "b"]


# --- mcp sdk level: real in-memory session ----------------------------------------


def test_mcp_list_tools_and_call_tool_over_memory_session():
    registry.register_module(obj_module())
    server = main.build_server()

    async def drive():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            opts = server.create_initialization_options()
            server_task = asyncio.create_task(
                server.run(*server_streams, opts, raise_exceptions=True)
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert [t.name for t in listed.tools] == ["find", "invoke"]
                    assert listed.tools[0].input_schema["type"] == "object"
                    result = await session.call_tool("find", {"id": "1234:5678"})
                    assert result.is_error is False
                    assert json.loads(result.content[0].text) == {"ok": True}
            finally:
                server_task.cancel()

    asyncio.run(drive())


def test_mcp_call_tool_unknown_tool_is_typed_error():
    registry.register_module(obj_module())
    server = main.build_server()

    async def drive():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            opts = server.create_initialization_options()
            server_task = asyncio.create_task(
                server.run(*server_streams, opts, raise_exceptions=True)
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    result = await session.call_tool("nope", {})
                    assert result.is_error is True
                    assert json.loads(result.content[0].text)["error"] == "UNKNOWN_TOOL"
            finally:
                server_task.cancel()

    asyncio.run(drive())


# --- production wiring (regression: boot served 0 tools) -----------------------


def test_production_bridge_registers_every_tool_module():
    import tools

    try:
        count = main._bridge_tools_to_registry()
        assert count == len(tools.names()) == len(registry.registered_tools())
        assert count > 0
        assert len({t.name for t in registry.visible_tools()}) == count
    finally:
        tools.clear()


def test_call_handler_supports_both_conventions():
    def kwargs_style(*, name: str = "x") -> dict:
        return {"ok": True, "name": name}

    def dict_style(args) -> dict:
        return {"ok": True, "echo": dict(args or {})}

    def defaults_style(args, _extra=None) -> dict:
        return {"ok": True}

    assert main._call_handler(kwargs_style, {"name": "y"}) == {"ok": True, "name": "y"}
    assert main._call_handler(dict_style, {"a": 1}) == {"ok": True, "echo": {"a": 1}}
    assert main._call_handler(dict_style, {}) == {"ok": True, "echo": {}}
    assert main._call_handler(defaults_style, {}) == {"ok": True}


def test_call_handler_does_not_swallow_real_type_errors():
    def broken(**kwargs) -> dict:
        raise TypeError("boom inside")

    with pytest.raises(TypeError, match="boom inside"):
        main._call_handler(broken, {})


def test_bridged_server_lists_and_calls_real_tool():
    import tools

    try:
        count = main._bridge_tools_to_registry()
        server = main.build_server()

        async def drive():
            async with create_client_server_memory_streams() as (client_streams, server_streams):
                opts = server.create_initialization_options()
                server_task = asyncio.create_task(
                    server.run(*server_streams, opts, raise_exceptions=True)
                )
                try:
                    async with ClientSession(*client_streams) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        assert len(listed.tools) == count
                        result = await session.call_tool(
                            "exclude_proc", {"names": ["nope.exe"]}
                        )
                        assert result.is_error is False
                        assert json.loads(result.content[0].text)["ok"] is True
                finally:
                    server_task.cancel()

        asyncio.run(drive())
    finally:
        tools.clear()
