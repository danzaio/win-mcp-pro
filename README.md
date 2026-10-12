# win-mcp-pro

**Windows UI Automation for MCP clients.**

![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![MCP](https://img.shields.io/badge/MCP-stdio-555555)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

Give your MCP client access to Windows controls, not just screenshots. win-mcp-pro reads the accessibility tree and finds controls by name or AutomationId. When a control exposes a UIA pattern, the client can use it to set a value, press a button or select an item.

The server runs locally over stdio. Its 53 registered tools cover UI inspection, window management and actions, with separate tools for screenshots and system tasks.

[Install](#install) · [Connect your client](#connect-your-client) · [Tools](#tools) · [Permissions](#permissions) · [Testing](#testing)

> [!IMPORTANT]
> Start with the read-only configuration below. Starting the server without tool filters exposes every tool, including PowerShell, file writes and process termination.

## Install

You need Windows 10 or 11, Python 3.11 or newer, and Git. The repository is currently private, so cloning requires GitHub access.

Run these commands in PowerShell:

```powershell
git clone https://github.com/danzaio/win-mcp-pro.git
cd win-mcp-pro
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

Use the virtual environment's interpreter directly. You don't need to run `Activate.ps1` or change your execution policy.

## Connect your client

For clients that use `mcpServers` configuration, use this read-only setup. Replace `C:\tools\win-mcp-pro` with your checkout location.

```json
{
  "mcpServers": {
    "win-mcp-pro": {
      "command": "C:\\tools\\win-mcp-pro\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "server.main",
        "--tools",
        "list_windows,snapshot,find,dump_tree,get_prop,get_patterns"
      ],
      "cwd": "C:\\tools\\win-mcp-pro"
    }
  }
}
```

Your client starts the server and manages its stdio connection. To check startup manually:

```powershell
.\.venv\Scripts\python.exe -m server.main --tools list_windows,snapshot,find,dump_tree,get_prop,get_patterns
```

The process waits for MCP requests. Logs go to stderr; stdout is reserved for the protocol.

### Working with a window

Start with `list_windows` to identify the application. Use `snapshot` to inspect its controls, then `find` to resolve the control you need. The returned ID works with `get_prop`, `get_patterns` and the action tools.

For example, this `tools/call` request finds an edit control whose AutomationId is `SearchBox`:

```json
{
  "name": "find",
  "arguments": {
    "automation_id": "SearchBox",
    "kind": "Edit"
  }
}
```

Check the returned control's handle and process ID before acting. For repeated snapshots of the same window, use `mode="full"` with its root ID; `mode="fast"` follows the foreground window.

The starter configuration doesn't allow writes or clicks. Add `set_value`, `invoke` or other action tools to `--tools` when your task needs them. After an action, read the control again to check the result.

## Tools

There are 53 registered tools across 10 modules. Your MCP client receives their argument schemas through `tools/list`.

| Group | Tools |
| --- | --- |
| Inspect the UI | `list_windows`, `snapshot`, `find`, `dump_tree`, `get_prop`, `get_patterns` |
| Control patterns | `invoke`, `toggle`, `select`, `expand`, `scroll_into_view`, `focus`, `set_value`, `get_value`, `paste_text`, `menu_click` |
| Input and clipboard | `click`, `move`, `scroll`, `shortcut`, `clipboard_get`, `clipboard_set` |
| Wait for a state | `wait`, `wait_text`, `wait_focus`, `wait_enabled`, `wait_gone` |
| Windows and processes | `app_launch`, `win_move`, `win_resize`, `win_min`, `win_max`, `win_restore`, `win_snap`, `get_hwnd_pid_path`, `proc_tree`, `is_elevated`, `integrity_level`, `exclude_proc`, `kill_proc` |
| System | `powershell`, `fs_list`, `fs_read`, `fs_write`, `registry_get`, `notify`, `scrape_dom`* |
| Transaction helpers | `preview`, `dry_run`, `before_after`, `profile` |
| Images | `screenshot`, `zoom` |

\* `scrape_dom` has no shipped browser backend and returns `NO_DOM_BACKEND` unless one is supplied separately.

To inspect the registry from the terminal:

```powershell
.\.venv\Scripts\python.exe -c "import tools; tools.load_all_modules(); print(sorted(tools.names()))"
```

## Permissions

Use `--tools` as an allowlist. `--exclude-tools` removes named tools and wins if a name appears in both lists. An explicitly empty filter is rejected; omitting both flags enables all registered tools.

These filters restrict which MCP tools a client can call. They are not an operating-system sandbox. PowerShell runs with the server process's permissions, and enabled UI tools can act on other applications.

Some tools accept `confirm=True`. That is an argument the caller can supply, not a human approval dialog. The transaction helpers don't gate direct UIA, input or system calls either; invoking `preview` does not authorize or protect a later action.

Run unelevated unless the target application requires elevation. Windows privilege boundaries can prevent interaction with elevated windows.

The server writes JSONL invocation metadata to stderr: tool names, argument field names and success or error events. It does not keep a durable action history or verify UI effects on your behalf.

## Compatibility

UI inspection depends on the application's UI Automation provider. Standard accessible controls are the intended target. Custom canvases, games and some Electron interfaces may expose little useful data.

`screenshot` and `zoom` are separate calls that save PNG files and return their paths. The server doesn't automatically switch to screenshots when UIA cannot find a control.

Snapshot caching is used only when the installed UIA library and control support it. Otherwise the tree is read directly. Time budgets are checked between UIA calls; they cannot interrupt a blocked COM call. `find` and `dump_tree` have depth and node limits, not hard wall-clock deadlines.

`exclude_proc` skips matching executable names in full-mode snapshot traversal, including explicit roots. Process-name lookup is best-effort, so exclusions are a traversal aid, not an access-control boundary.

Browser DOM automation and a network transport are not included. This package serves a local MCP client over stdio.

## Testing

Install the development extra and run the unit and offline runner tests:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest tests eval/test_smoke.py -q
```

The local validation on 12 October 2026 passed **376 tests**, with the opt-in live test skipped in that run. That live test was run separately and passed. A built wheel was also installed in a clean environment and checked through a real MCP stdio connection.

The live test creates its own disposable WinForms window. It checks scoped snapshots, value writes and button invocation with fresh readback, then closes its own processes. It is not a claim that every Windows application has been tested.

To run it:

```powershell
$env:WINMCP_RUN_LIVE_UIA = "1"
try {
    .\.venv\Scripts\python.exe -m pytest tests/test_live_uia.py -q
} finally {
    $env:WINMCP_RUN_LIVE_UIA = $null
}
```

The 12-task eval uses `FakeClient` fixtures. It checks the runner and its expectations, not live desktop performance:

```powershell
.\.venv\Scripts\python.exe eval/run.py --min-success 0.8 --out "$env:TEMP\win-mcp-pro-eval.json"
```

The [CI workflow](.github/workflows/eval-gate.yml) runs the Windows unit suite and offline eval checks. It does not run the opt-in live UIA test. The current local candidate has not yet been run through GitHub Actions.

## Documentation

- [Operations guide](docs/SOP.md): configuration, troubleshooting and deployment notes.
- [Terminal demo](demo/script.md): commands for a short walkthrough.
- [Tool implementations](tools): schemas and handlers for each tool group.

## License

[MIT](LICENSE).
