# SOP — win-mcp-pro (handoff)

One page. Install it, run it, keep it alive. Owner: whoever is on call for the automation box.

## Install

Needs Windows 10/11, Python 3.11+, git. Nothing else.

```
git clone https://github.com/danzaio/win-mcp-pro.git
cd win-mcp-pro
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -c "import server"     # smoke test, must exit 0
.venv\Scripts\python -m pytest tests -q     # suite
```

Use the venv interpreter directly instead of running `Activate.ps1`. Some machines block that script through execution policy and the failure looks like a broken repo.

If `import server` fails, the venv is incomplete. Redo `pip install -e ".[dev]"` and read the error, it is almost always a missing dependency.

## Run

The server is a foreground stdio process. The MCP client starts it, not you.

```
.venv\Scripts\python -m server.main --tools list_windows,snapshot,find,dump_tree,get_prop,get_patterns
```

Flags: `--tools` and `--exclude-tools`, both comma-separated, both also accept the `--tools=a,b` form. Point the client at the **venv interpreter**, never a bare `python`:

```json
{
  "mcpServers": {
    "win-mcp-pro": {
      "command": "C:\\path\\to\\win-mcp-pro\\.venv\\Scripts\\python.exe",
      "args": ["-m", "server.main", "--tools", "list_windows,snapshot,find,dump_tree,get_prop,get_patterns"],
      "cwd": "C:\\path\\to\\win-mcp-pro"
    }
  }
}
```

stdout is the JSON-RPC channel. Startup and tool-call metadata go to stderr as JSONL. Calls log the tool name and argument field names, not values or verified UI effects. The log is not durable unless the client captures it. One stray `print` to stdout corrupts the session.

## Allowlist

Two flags, comma-separated: `--tools a,b,c` (only these load) and `--exclude-tools x,y` (everything except these). Both also accept the `--tools=a,b` form. Enforcement lives in `server/security.py:enforce_filter`.

Three rules that hold:

- **Blocklist wins over allowlist.** A tool named in `--exclude-tools` stays hidden even if `--tools` also names it.
- **Omitting a flag means no restriction from it.** With neither flag, every registered tool is exposed.
- **An explicit empty list is rejected.** `--tools ""` and `--tools ,` stop with a CLI error; they do not mean "allow everything" or "allow nothing".

No filters means **all registered tools are exposed**, including PowerShell, filesystem writes, process termination, clipboard, window, and UI action tools. A blocklist of the four compat tools is not a read-only policy.

Recommended starting posture for a box doing real work:

```
--tools list_windows,snapshot,find,dump_tree,get_prop,get_patterns
```

This profile exposes tree inspection only. Add action or system tools explicitly for a task. `confirm=True` on some tools is an argument supplied by the MCP caller, not a human approval step. If a tool is missing from the client, check the allowlist before debugging the server.

## Admin and UIPI

UIA enumerates more when the process is elevated, and an **unelevated server cannot send input to an elevated window** (UIPI). Read-only inspection works either way.

- Normal day: run unelevated.
- Target window is elevated: restart the terminal as Administrator and start the server from it.
- The server may return `ELEVATION_REQUIRED` when integrity levels do not line up. UIA calls are synchronous; a traversal budget cannot interrupt a COM call that is already blocked.

## When to use the vision fallback

`screenshot` and `zoom` capture the selected display and save PNGs under the system temp directory. Their calls do not record a reason, and no cost or human-confirmation gate is enforced. They are excluded by the read-only allowlist above.

- Canvas, WebGL, or a custom-rendered surface with no UIA provider.
- An icon or glyph with no accessible name at all.
- A target `find` returns `NOT_FOUND` for, after you confirmed the element exists on screen.

Before you fall back, rule out the boring causes: the element really has a provider (check with Inspect.exe from the Windows SDK, or Accessibility Insights), and `find` was given the right name or AutomationId. Do not rely on `exclude_proc` or a traversal budget to interrupt a synchronous COM call that is already blocked.

## Support checklist

Work top to bottom. Do not skip.

1. `python -c "import server"` — a failure here is venv or install, never the server.
2. Tools missing from the client — check `--tools` / `--exclude-tools` first, then confirm the tool is actually registered: `.venv\Scripts\python -c "import tools; tools.load_all_modules(); print(sorted(tools.names()))"`. A tool that is not in that list has not landed, and no allowlist will conjure it.
3. Client says the server failed to start — the command is not the venv python, or the cwd is wrong.
4. Session dies right after start — something wrote to stdout. Search the code for a bare `print`.
5. A collection error in the full test suite is a blocker in this checkout. Do not skip the affected tool group.
6. `find` returns `NOT_FOUND` — verify the app has a provider with Inspect.exe. If it does, the query is wrong.
7. Action returns `ELEVATION_REQUIRED` — elevation mismatch. Restart elevated or pick another target.
8. A UIA call is slow — the snapshot budget is cooperative between nodes, not a hard timeout for synchronous COM calls. `find`/`dump_tree` have node/depth caps but no wall-clock deadline. Do not claim a timed-out COM call was safely skipped.
9. An action appears to have no effect: stderr records the tool and field names, not the resulting UI state. Read the tool response and independently inspect the target; the stderr line is not proof of an effect.
10. `transact` helpers are separate tools. Direct UIA and compatibility actions do not go through their preview or deduplication pipeline.
11. Client connects but sees **zero tools**: check the `--tools` allowlist first, then inspect the `server_start` stderr event and run the registry count command in step 2. Import failures stop server startup.
12. `scrape_dom` returns `NO_DOM_BACKEND`: expected in this package; a production browser adapter is not included.
13. Reproduce the offline smoke set with `python eval/run.py --out <temp-path>`. Its canned client is not a live desktop test.

## Repo hygiene

- `@AutomationLog.txt` may be emitted by the UIA COM path; it is ignored by git.
- `eval/results.json` is ignored generated output and contains fake-client results. Do not treat it as a measurement or overwrite a local copy when its contents matter; use `--out` to write elsewhere.

## Known gaps

- No UIA provider means invisible. By design and documented, not a defect to chase.
- There is no live UI evaluation or browser DOM backend in this package. The golden set runs against canned responses.
- Text selection through the tree is not supported (inherited UIA limitation).
- Type with `set_value` or `paste_text`. There is no character-by-character typing.
- Adding a tool: create `tools/<group>.py` exposing `TOOLS` with `name`, `description`, `schema` (a pydantic model class) and `handler`. `tools.load_all_modules()` finds it by filename. The registry is the single source of truth, in registration order.
- v1 is stdio and single-client. Remote use needs the documented Streamable HTTP extension with auth, TLS, and an IP allowlist.
