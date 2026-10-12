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
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pydantic import BaseModel

from server import audit, main, registry


def _process_is_running(pid: int) -> bool:
    """Check a subprocess after stdio_client has completed bounded shutdown."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return state == 0x00000102  # WAIT_TIMEOUT means still running
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


def test_mcp_call_serializes_pydantic_handler_results_as_json_objects():
    class ResultModel(BaseModel):
        value: str

    registry.register_module(
        [AttrToolDef("model_result", handler=lambda **_: ResultModel(value="from-uia"))]
    )
    server = main.build_server()

    async def drive():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            server_task = asyncio.create_task(
                server.run(
                    *server_streams,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    result = await session.call_tool("model_result", {})
                    assert result.is_error is False
                    assert json.loads(result.content[0].text) == {"value": "from-uia"}
            finally:
                server_task.cancel()

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("allowlist", "blocklist"),
    [
        (["find"], None),
        (None, ["invoke"]),
        (["invoke"], ["invoke"]),
    ],
    ids=["allowlist-denies", "blocklist-denies", "blocklist-wins-conflict"],
)
def test_mcp_filtered_call_never_dispatches_denied_handler(allowlist, blocklist):
    calls = []

    def spy(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    registry.register_module(
        [AttrToolDef("find", handler=spy), AttrToolDef("invoke", handler=spy)]
    )
    server = main.build_server(allowlist=allowlist, blocklist=blocklist)

    async def drive():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            server_task = asyncio.create_task(
                server.run(
                    *server_streams,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert "invoke" not in [tool.name for tool in listed.tools]
                    result = await session.call_tool("invoke", {"id": "probe"})
                    assert result.is_error is True
                    assert json.loads(result.content[0].text)["error"] == "UNKNOWN_TOOL"
            finally:
                server_task.cancel()

    asyncio.run(drive())
    assert calls == []


def test_stdio_handler_exception_keeps_server_alive_and_stdout_protocol_clean(tmp_path):
    from mcp.client.stdio import StdioServerParameters, stdio_client

    project_root = Path(__file__).resolve().parents[1]
    server_pid_file = tmp_path / "stdio-server.pid"
    server_exit_file = tmp_path / "stdio-server.exited"
    child_script = textwrap.dedent(
        f"""
        import atexit
        import asyncio
        import os
        from pathlib import Path
        from pydantic import BaseModel
        from server import registry
        from server.audit import audit
        from server.main import _bridge_tools_to_registry, _serve, build_server

        server_exit_file = Path({str(server_exit_file)!r})
        Path({str(server_pid_file)!r}).write_text(str(os.getpid()), encoding="ascii")
        atexit.register(lambda: server_exit_file.write_text("exited", encoding="ascii"))

        class ProbeArgs(BaseModel):
            fail: bool

        def fault_probe(fail: bool):
            if fail:
                raise RuntimeError("synthetic stdio probe failure")
            return {{"ok": True}}

        _bridge_tools_to_registry()
        registry.register_module([{{
            "name": "fault_probe",
            "description": "test-only handler failure probe",
            "schema": ProbeArgs,
            "handler": fault_probe,
        }}])
        visible = ["fault_probe", "exclude_proc"]
        audit({{"event": "server_start", "allowlist": visible, "tools": 2}})
        asyncio.run(_serve(build_server(allowlist=visible)))
        """
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", child_script],
        cwd=str(project_root),
    )

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_log:
        async def drive():
            async with stdio_client(params, errlog=stderr_log) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert {tool.name for tool in listed.tools} == {
                        "fault_probe",
                        "exclude_proc",
                    }

                    failed = await session.call_tool("fault_probe", {"fail": True})
                    assert failed.is_error is True
                    failure = json.loads(failed.content[0].text)
                    assert failure["error"] == "RuntimeError"
                    assert failure["message"] == "synthetic stdio probe failure"

                    recovered = await session.call_tool(
                        "exclude_proc",
                        {"names": ["win-mcp-pro-stdio-probe.exe"]},
                    )
                    assert recovered.is_error is False
                    assert json.loads(recovered.content[0].text) == {
                        "ok": True,
                        "excluded": ["win-mcp-pro-stdio-probe"],
                    }

        try:
            asyncio.run(asyncio.wait_for(drive(), timeout=30))
        except Exception as exc:
            stderr_log.seek(0)
            raise AssertionError(
                f"stdio client failed: {exc}\nchild stderr:\n{stderr_log.read()}"
            ) from exc
        stderr_log.seek(0)
        audit_lines = [
            json.loads(line)
            for line in stderr_log.read().splitlines()
            if '"event"' in line
        ]

    events = [(event.get("event"), event.get("tool")) for event in audit_lines]
    assert ("tool_error", "fault_probe") in events
    assert ("tool_ok", "exclude_proc") in events
    server_pid = int(server_pid_file.read_text(encoding="ascii"))
    assert server_exit_file.read_text(encoding="ascii") == "exited"
    assert not _process_is_running(server_pid), "stdio server process survived client shutdown"


def test_mcp_fs_write_and_kill_proc_refusals_have_no_side_effects(
    monkeypatch, tmp_path
):
    from tools import system, winproc

    target = tmp_path / "must-not-exist" / "probe.txt"
    write_calls = []
    resolve_calls = []
    terminate_calls = []

    def write_spy(*args, **kwargs):
        write_calls.append((args, kwargs))
        raise AssertionError("fs_write reached its file-open boundary")

    def resolve_spy(*args, **kwargs):
        resolve_calls.append((args, kwargs))
        raise AssertionError("fs_write reached path resolution")

    def terminate_spy(*args, **kwargs):
        terminate_calls.append((args, kwargs))
        raise AssertionError("kill_proc reached process termination")

    monkeypatch.setattr(system, "open", write_spy, raising=False)
    monkeypatch.setattr(system, "_resolve_confined", resolve_spy)
    monkeypatch.setattr(winproc, "_terminate_pid", terminate_spy)
    registry.register_module(
        [
            next(tool for tool in system.TOOLS if tool["name"] == "fs_write"),
            next(tool for tool in winproc.TOOLS if tool["name"] == "kill_proc"),
        ]
    )
    server = main.build_server()

    async def drive():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            server_task = asyncio.create_task(
                server.run(
                    *server_streams,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()

                    invalid_write = await session.call_tool(
                        "fs_write", {"path": str(target)}
                    )
                    assert invalid_write.is_error is True

                    refused_write = await session.call_tool(
                        "fs_write",
                        {
                            "path": str(target),
                            "content": "must not be written",
                            "confirm": False,
                        },
                    )
                    write_result = json.loads(refused_write.content[0].text)
                    assert write_result["ok"] is False
                    assert write_result["error"] == "CONFIRM_REQUIRED"

                    invalid_kill = await session.call_tool(
                        "kill_proc", {"pid": "not-a-pid", "confirm": True}
                    )
                    assert invalid_kill.is_error is True

                    refused_kill = await session.call_tool(
                        "kill_proc", {"pid": 2_000_000_000, "confirm": False}
                    )
                    assert refused_kill.is_error is True
                    assert json.loads(refused_kill.content[0].text)["error"] == (
                        "CONFIRM_REQUIRED"
                    )
            finally:
                server_task.cancel()

    asyncio.run(drive())
    assert write_calls == []
    assert resolve_calls == []
    assert terminate_calls == []
    assert not target.exists()
    assert not target.parent.exists()


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


def test_call_handler_does_not_retry_handler_after_internal_type_error():
    calls = 0

    def broken(args):
        nonlocal calls
        calls += 1
        raise TypeError("failure inside handler")

    with pytest.raises(TypeError, match="failure inside handler"):
        main._call_handler(broken, {"args": "payload"})

    assert calls == 1


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


@pytest.mark.skipif(sys.platform != "win32", reason="production tool modules require Windows")
def test_stdio_subprocess_lists_and_calls_a_desktop_independent_tool(tmp_path):
    from mcp.client.stdio import StdioServerParameters, stdio_client

    project_root = Path(__file__).resolve().parents[1]
    server_pid_file = tmp_path / "production-server.pid"
    server_exit_file = tmp_path / "production-server.exited"
    child_script = textwrap.dedent(
        f"""
        import atexit
        import os
        import runpy
        import sys
        from pathlib import Path

        server_exit_file = Path({str(server_exit_file)!r})
        Path({str(server_pid_file)!r}).write_text(str(os.getpid()), encoding="ascii")
        atexit.register(lambda: server_exit_file.write_text("exited", encoding="ascii"))
        sys.argv = ["server.main", "--tools", "exclude_proc"]
        runpy.run_module("server.main", run_name="__main__")
        """
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", child_script],
        cwd=str(project_root),
    )

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_log:
        async def drive():
            async with stdio_client(params, errlog=stderr_log) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert [tool.name for tool in listed.tools] == ["exclude_proc"]

                    result = await session.call_tool(
                        "exclude_proc", {"names": ["win-mcp-pro-e2e-probe.exe"]}
                    )
                    assert result.is_error is False
                    assert json.loads(result.content[0].text) == {
                        "ok": True,
                        "excluded": ["win-mcp-pro-e2e-probe"],
                    }

        asyncio.run(asyncio.wait_for(drive(), timeout=30))
        stderr_log.seek(0)
        assert '"event": "server_start"' in stderr_log.read()

    server_pid = int(server_pid_file.read_text(encoding="ascii"))
    assert server_exit_file.read_text(encoding="ascii") == "exited"
    assert not _process_is_running(server_pid), "stdio server process survived client shutdown"
