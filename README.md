# win-mcp-pro

**A small Windows UI Automation MCP server you can read in an afternoon.**
Every input is bound to an element or capture it was verified against. When a
check fails, the call is refused and nothing is sent.

[![ci](https://github.com/danzaio/win-mcp-pro/actions/workflows/ci.yml/badge.svg)](https://github.com/danzaio/win-mcp-pro/actions/workflows/ci.yml)
![Windows 10 | 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
[![MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

![An MCP client talks to win-mcp-pro over stdio; the server reads and drives app controls through Windows UI Automation.](docs/diagrams/architecture.svg)

## Try it in one line

Read-only first. This exposes inspection and waiting only: no clicks, no
typing, no clipboard, no files.

```powershell
uvx --from git+https://github.com/danzaio/win-mcp-pro win-mcp-pro --read-only
```

Add it to an MCP client (Claude Desktop, Cursor, Claude Code, any stdio client):

```json
{
  "mcpServers": {
    "win-mcp-pro": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/danzaio/win-mcp-pro", "win-mcp-pro", "--read-only"]
    }
  }
}
```

Needs Windows 10 or 11 and [uv](https://docs.astral.sh/uv/). Python 3.11 or
newer is enough; uv fetches it if you do not have it. To let the client act,
replace `--read-only` with an explicit list, for example
`--tools list_windows,find,get_value,set_value,invoke`.

## What it looks like

A real session against Notepad, driven by an MCP client. Output is trimmed with
`...`; nothing else is changed. The machine runs Windows in Portuguese, so the
window is titled `Sem título - Bloco de Notas`.

```text
tools/list   -> ['click', 'find', 'get_value', 'list_windows', 'set_value']
list_windows -> {"hwnd": 20449106, "title": "Sem título - Bloco de Notas", "pid": 75792, "visible": true}
find Edit    -> {"ok": true, "element": {"id": "20449106:42,46794936", "kind": "Edit", ...}}
set_value    -> {"ok": true, "before": "", "after": "hello from an MCP client", "verified": true}
get_value    -> {"ok": true, "value": "hello from an MCP client"}
click x=10 y=10 with no frame or target ->
  {"ok": false, "error": "FRAME_REQUIRED", "reason": "coordinate input needs the frame id
   from a fresh screenshot/zoom (pass frame=..., or start the server with
   --input-unrestricted for raw desktop coordinates)"}
```

`set_value` reads the control back and says whether it took. The blind
`click` is refused before any event is sent.

## Why another one

The most-used Windows MCP server is about 24k lines of Python. This one aims to
be small enough to read before you let it drive your desktop, because it can
act as you.

|  | win-mcp-pro | [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) v0.8.8 |
| --- | --- | --- |
| Source size | 21 files, 6.0k lines of code | 80 files, 24.0k lines of code |
| Python | 3.11+ | 3.14+ |
| Tools | 54 | 21 |

Source size is lines of Python excluding blanks and comments, counted on
2026-10-20. It is a measure of how much you have to read, not of quality.
This one has **more** tools, not fewer: most are small and single-purpose, and
that is a larger surface to review. It is why `--read-only` and `--tools`
exist. Windows-MCP is the more widely used project and has features this one
does not, including a desktop overlay and HTTP transports.

What this one does that is worth checking for yourself:

- **Refusals send nothing.** Typed errors (`FRAME_REQUIRED`, `FRAME_STALE`,
  `TARGET_MISMATCH`, `OCCLUDED`, `ELEVATION_REQUIRED`, ...) come back before
  any input event. `tests/test_input_binding.py` proves it with a spy on
  `SendInput`.
- **UIA patterns before pixels.** The client reads the accessibility tree and
  uses `invoke`, `set_value` and friends. Coordinates are the fallback, and they
  must be tied to a fresh capture and the element they intend.
- **A read-only mode and an allowlist.** `--read-only`, `--tools` and
  `--exclude-tools` decide what a client can even see.
- **A bench with an outside witness.** `bench/run_matrix.py` drives disposable
  windows and checks each effect in the fixture's own log, not in the server's
  reply. A decoy window proves nothing leaks to the wrong target.

## Safety model

| Flag | Effect |
| --- | --- |
| `--read-only` | Inspection and waiting only (17 tools). |
| `--tools a,b,c` | Only these tools exist for the client. |
| `--exclude-tools x,y` | Everything except these. Wins over `--tools`. |
| `--input-unrestricted` | Allow raw coordinates and unbound keys. Off by default. |
| `--uia-worker` | Run tree walks in a child process that is killed on a stuck call. |

An empty list is rejected rather than read as "allow everything". Filtered-out
tools are absent from `tools/list` and answer `UNKNOWN_TOOL` if called anyway.

**This is a policy, not a sandbox.** An allowed tool runs with your
permissions. `confirm=True` on a tool is an argument the client supplies, not
a human approval prompt. See [SECURITY.md](SECURITY.md).

Logs go to stderr as JSON lines: tool names and argument names, never values.

## Tools

54 tools. Your client gets each schema from `tools/list`.

| Group | Tools |
| --- | --- |
| Inspect | `list_windows` `get_active_window` `snapshot` `find` `dump_tree` `get_prop` `get_patterns` `get_value` `table_extract` |
| Act through UIA patterns | `invoke` `toggle` `select` `expand` `scroll_into_view` `focus` `set_value` `paste_text` `menu_click` `form_fill` `choose_file` |
| Input by coordinates | `click` `double_click` `move` `scroll` `shortcut` `type_text` `key_hold` `cursor_position` `clipboard_get` `clipboard_set` |
| Wait | `wait` `wait_text` `wait_focus` `wait_enabled` `wait_gone` `wait_change` |
| Windows and apps | `app_launch` `open_default` `win_move` `win_resize` `win_min` `win_max` `win_restore` `win_snap` `win_close` `exclude_proc` |
| Pixels | `screenshot` `zoom` `win_shot` `displays` `ocr_read` `ocr_click` |
| Flows and health | `replay` `doctor` |

Typical flow: `list_windows`, then `snapshot` or `find`, then an action, then
read the control again to confirm. `replay` re-runs a recorded flow and stops
at the first step whose result differs (`DRIFT`).

## Limits

- It only sees what an app exposes through UI Automation. Games, canvases and
  some Electron or custom-drawn UIs expose little. `ocr_read` reads pixels as a
  fallback; the server never switches to screenshots on its own.
- An unelevated server cannot send input to an elevated window (Windows UIPI).
  It reports `ELEVATION_REQUIRED`. Run it unelevated unless you must.
- Time budgets are checked between UIA calls. They cannot interrupt a COM call
  that is already blocked; `--uia-worker` exists for that.
- stdio only, one local client. No network transport.
- Tested on Windows 10 Pro (build 19045) with Notepad and disposable WinForms
  and Tk fixtures. Windows 11 is supported by the API but untested by me. That
  is not a claim that every application works.

## Troubleshooting

- **A tool is missing in the client.** Check `--tools`, `--exclude-tools` and
  `--read-only` first, then list what is registered:
  `python -c "from win_mcp_pro import tools; tools.load_all_modules(); print(sorted(tools.names()))"`
- **The client says the server died right after start.** Something wrote to
  stdout, which is the protocol channel. Logs belong on stderr.
- **`find` returns `NOT_FOUND` for something you can see.** The app probably
  has no UI Automation provider. Check with Inspect.exe from the Windows SDK or
  Accessibility Insights before blaming the query.
- **`ELEVATION_REQUIRED`.** The target runs elevated and the server does not.
  Restart the server elevated, or pick another target.

## Develop

```powershell
git clone https://github.com/danzaio/win-mcp-pro.git
cd win-mcp-pro
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```

The unit suite runs against fakes, so it never touches your desktop. The bench
does, and moves your cursor: run it on an idle machine.

```powershell
.venv\Scripts\python.exe bench\run_matrix.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and
[CHANGELOG.md](CHANGELOG.md). Licensed under [MIT](LICENSE).
