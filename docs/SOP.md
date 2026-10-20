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
.venv\Scripts\python -m win_mcp_pro.server.main --tools list_windows,snapshot,find,dump_tree,get_prop,get_patterns
```

Flags: `--tools` and `--exclude-tools`, both comma-separated, both also accept the `--tools=a,b` form. Point the client at the **venv interpreter**, never a bare `python`:

```json
{
  "mcpServers": {
    "win-mcp-pro": {
      "command": "C:\\path\\to\\win-mcp-pro\\.venv\\Scripts\\python.exe",
      "args": ["-m", "win_mcp_pro.server.main", "--tools", "list_windows,snapshot,find,dump_tree,get_prop,get_patterns"],
      "cwd": "C:\\path\\to\\win-mcp-pro"
    }
  }
}
```

stdout is the JSON-RPC channel. Startup and tool-call metadata go to stderr as JSONL. Calls log the tool name and argument field names, not values or verified UI effects. The log is not durable unless the client captures it. One stray `print` to stdout corrupts the session.

## Allowlist

Two flags, comma-separated: `--tools a,b,c` (only these load) and `--exclude-tools x,y` (everything except these). Both also accept the `--tools=a,b` form. Enforcement lives in `win_mcp_pro/server/security.py:enforce_filter`.

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

## Input binding

Coordinate input is bound by default (the *safe profile*):

- `click` with x/y needs `frame=` (from a fresh `screenshot`/`zoom`; x/y are image pixels of that capture) and `target=` (the element the click intends). With `target` only, the click goes to the target's center.
- Before any event is sent, the point is translated to desktop coordinates, checked against the target's rectangle, hit-tested (`WindowFromPoint`), and the target's integrity level is checked. Mismatches return typed errors - `FRAME_REQUIRED`, `FRAME_STALE`, `FRAME_UNKNOWN`, `TARGET_REQUIRED`, `TARGET_MISMATCH`, `OCCLUDED`, `ELEVATION_REQUIRED`, `INTEGRITY_CHECK_FAILED` - with **zero input sent**.
- `shortcut` requires `target=` and brings its window to the foreground first when needed (`activate=false` refuses instead; `ACTIVATION_FAILED` when the OS foreground lock refuses).
- `scroll` uses the target center or x/y of the frame; drags need `from_x/from_y` plus target/frame so both endpoints verify. A plain unbound `move` still works for hovering.
- `--input-unrestricted` starts the legacy profile (raw coordinates, unbound keys) for scripts that need it. Frames are still validated whenever provided.

Frames expire after 30 seconds and only the last 16 are kept. The server runs with per-monitor DPI awareness so UIA rectangles, captures and input share one physical-pixel space; absolute input spans the full virtual desktop (negative monitor coordinates included).

## Worker mode

Opt-in via `--uia-worker`: the tree walks (`snapshot`, `find`, `dump_tree`) run in a supervised child process (`server.worker`) instead of the server process.

- One walk at a time; the deadline is `--worker-timeout-ms` (default 15000).
- On deadline the worker is killed and the call returns `WORKER_TIMEOUT` (read-only: result unknown, zero side effects, nothing retried); the next call spawns a fresh worker.
- Crash loop: after repeated spawns inside a minute the supervisor refuses with `WORKER_UNAVAILABLE` until the window passes.
- Worker died mid-call: `WORKER_LOST`; nothing is retried.
- The worker exits on stdin EOF and the server kills it on shutdown - no orphans.
- Ids minted before a kill keep resolving when they carry native identity (`hwnd:runtimeid`); digest-only ids may come back `NOT_FOUND`.
- Input never runs in the worker; safe-profile verification stays in the server process, so a killed worker can never abandon a half-sent gesture.

## Browser mode

The `browser_*` tools drive an isolated Chromium (Edge/Chrome) over the DevTools protocol.

- Launch: the first `browser_open` starts the browser headless with a disposable profile under the temp dir; `headless=false` shows the window instead. `browser_close` kills it and deletes the profile; an `atexit` hook does the same when the server exits.
- Refs: `browser_snapshot` lists accessibility nodes with refs (`b1`, ...). `browser_click` / `browser_fill` / `browser_read` take a ref; an unknown ref refuses (`UNKNOWN_REF`) without touching the browser, and a node that no longer resolves refuses as `STALE_REF`.
- No OS input: clicks are `Input.dispatchMouseEvent` inside the browser; the desktop cursor and foreground window are never touched.
- The user's own browser profile is never used or attached; there is no option to attach to it in this version.
- `WINMCP_BROWSER_PATH` points at a specific Chromium binary when auto-detection fails.

## Replay and doctor

`replay` re-runs a recorded flow with assertions: each step names a tool, its args (a string arg `@id:N` binds the element id produced by step N) and optional expected top-level result values. A mismatch stops with `DRIFT` (step, key, expected, actual); an unresolvable binding refuses as `AMBIGUOUS_ID`; a step that answered `ok=false` without expectations is drift too. `doctor` reports environment readiness (interpreter, dependencies, Chromium, OCR languages, UIA). `docs/parity.md` maps every capability to the test or bench task that proves it.

## Bench matrix

`python bench/run_matrix.py` runs the driver-conformance matrix: disposable WinForms + Tk fixtures log every observable effect to their own JSONL files and the runner asserts against those logs (external oracle), with a decoy fixture proving zero wrong-target leakage. Results land in `bench/results/`; the exit code is non-zero if any task fails. It is opt-in and not part of CI (CI runs the unit suite and the offline eval). See `bench/README.md`.

## When to use the vision fallback

`screenshot` and `zoom` capture the selected display and save PNGs under the system temp directory. When the PNG fits the inline cap (4 MB), the same bytes are delivered as an MCP image content block; over the cap the result keeps the path only, with a reason. Each result reports the display origin and a short-lived frame id (`click`/`drag`/`scroll` accept `frame=...`); stale frames are refused with `FRAME_STALE` before any input. Their calls do not record a reason, and no cost or human-confirmation gate is enforced. They are excluded by the read-only allowlist above.

`ocr_read` captures a monitor or region and recognizes its text with Windows OCR (`winsdk`); the result carries per-line and per-word bounding boxes plus a frame id, so a word box center can be clicked through the frame-pinned path. Without an OCR language pack for the user profile it returns `NO_OCR_LANGUAGE`.

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
12. `scrape_dom` returns `NO_DOM_BACKEND`: expected - it is a stub. Use the `browser_*` tools for DOM automation; they drive an isolated browser and never attach to your own session.
13. Reproduce the offline smoke set with `python eval/run.py --out <temp-path>`. Its canned client is not a live desktop test.

## Repo hygiene

- `@AutomationLog.txt` may be emitted by the UIA COM path; it is ignored by git.
- `eval/results.json` is ignored generated output and contains fake-client results. Do not treat it as a measurement or overwrite a local copy when its contents matter; use `--out` to write elsewhere.

## Known gaps

- No UIA provider means invisible. By design and documented, not a defect to chase.
- There is no live UI evaluation in this package. The golden set runs against canned responses. Browser DOM automation exists through the isolated `browser_*` tools (their live test is opt-in, like the UIA ones).
- Text selection through the tree is not supported (inherited UIA limitation).
- Type with `set_value` or `paste_text`. There is no character-by-character typing.
- Adding a tool: create `tools/<group>.py` exposing `TOOLS` with `name`, `description`, `schema` (a pydantic model class) and `handler`. `tools.load_all_modules()` finds it by filename. The registry is the single source of truth, in registration order.
- v1 is stdio and single-client. Remote use needs the documented Streamable HTTP extension with auth, TLS, and an IP allowlist.
