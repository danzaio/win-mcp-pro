"""Opt-in live browser fixture (Phase C).

Run explicitly::

    WINMCP_RUN_LIVE_BROWSER=1 python -m pytest tests/test_live_browser.py -q

Launches the installed Edge/Chrome headless with a disposable profile (no
user session, no network: the page is a ``data:`` URL), drives it through
the MCP stdio route, and asserts the click effect from the page itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from urllib.parse import quote

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("WINMCP_RUN_LIVE_BROWSER") != "1",
    reason="opt-in live browser smoke; requires an installed Edge/Chrome",
)

PAGE = (
    "<html><head><title>WinMCP Browser Probe</title></head><body>"
    "<div id='out'>before</div>"
    "<button id='b' onclick=\"document.getElementById('out').textContent='after'\">"
    "Go</button></body></html>"
)
PAGE_URL = "data:text/html," + quote(PAGE, safe="")


def _tool_json(result) -> dict:
    assert result.is_error is False, result.content
    assert result.content and result.content[0].type == "text"
    value = json.loads(result.content[0].text)
    assert isinstance(value, dict)
    return value


def test_live_isolated_browser_dom_flow():
    project_root = Path(__file__).resolve().parents[1]
    allowed = ("browser_open,browser_status,browser_snapshot,browser_click,"
               "browser_read,browser_close")
    server_script = textwrap.dedent(
        f"""
        import runpy
        import sys

        sys.argv = ["server.main", "--tools", {allowed!r}]
        runpy.run_module("server.main", run_name="__main__")
        """
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", server_script],
        cwd=str(project_root),
    )

    async def drive() -> None:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()

                    async def call(name: str, args: dict) -> dict:
                        return _tool_json(await session.call_tool(name, args))

                    before = await call("browser_status", {})
                    assert before["ok"] is True and before["running"] is False

                    opened = await call("browser_open", {"url": PAGE_URL})
                    assert opened["ok"] is True, opened
                    assert opened["pid"] and opened["pid"] > 0, opened
                    assert opened["page"]["title"] == "WinMCP Browser Probe", opened
                    assert opened["page"]["ready"] == "complete", opened

                    snap = await call("browser_snapshot", {})
                    assert snap["ok"] is True, snap
                    go = next((r for r in snap["refs"] if r["name"] == "Go"), None)
                    assert go is not None, snap

                    clicked = await call("browser_click", {"ref": go["ref"]})
                    assert clicked["ok"] is True, clicked

                    read = await call("browser_read", {})
                    assert read["ok"] is True, read
                    assert "after" in read["text"], read

                    closed = await call("browser_close", {})
                    assert closed["ok"] is True and closed["closed"] is True

                    after = await call("browser_status", {})
                    assert after["running"] is False

    asyncio.run(asyncio.wait_for(drive(), timeout=180))
