# win-mcp-pro

A Windows computer-use MCP server that reads the UI as a **tree**, not a picture. It finds controls by name and AutomationId, acts through Control Patterns, and writes an audit row for every call. Screenshots exist only as an opt-in fallback.

> **Status: v1 in progress.** This repo is being built task by task against
> `docs/superpowers/plans/2026-10-10-win-mcp-pro.md`. Every number that has not been
> measured yet is written as `PENDING-RUN`. Nothing below is invented.

---

## 1. The problem

Most Windows computer-use agents take a screenshot, ask a vision model where the button is, then click those coordinates. It demos well and falls apart on a real desktop.

- **Cost.** A screenshot pushes the entire desktop into the model context on every single step. The tree pushes the few hundred elements that actually matter.
- **Drift.** Coordinates go stale. A window moves, a toast pops over the button, an animation finishes, and the click lands on nothing.
- **No audit trail.** "Clicked (1042, 388)" is not a fact you can review two weeks later. "Invoked the Save button in Notepad's File menu" is.

Windows has had the answer since Vista: UI Automation. Every standard control reports a name, an AutomationId, a control type, and a set of Control Patterns. That is the same data a screen reader consumes, which is why it is stable, typed, and pure text.

win-mcp-pro automates against that tree: **snapshot once, resolve a stable id, act by pattern.** Image is the fallback, never the default.

## 2. Architecture

```
 MCP client (Claude Desktop / Cursor / OpenCode)
      |
      |  JSON-RPC 2.0 over stdio.  Logs go to stderr only.
      v
 +--------------------------------------------------------------------+
 | win-mcp-pro    (python -m server.main)                              |
 |                                                                    |
 |  --tools a,b,c  /  --exclude-tools x,y  ->  security.py (allowlist)  |
 |  every tool call                        ->  audit.py   (stderr JSONL)|
 |                                                                    |
 |    1 SNAPSHOT            2 FIND                3 ACT                |
 |    one CacheRequest  ->  stable id + path ->  Control Pattern call   |
 |    tree, props,         from the window      (Invoke, Value,         |
 |    patterns             down to the child    Selection, Expand,      |
 |                                             Window, Scroll, Text)   |
 |                                                                    |
 |    tools/snapshot.py    tools/query.py     tools/patterns.py        |
 |                                          tools/values.py            |
 |                                          tools/compat.py (opt-in)   |
 |                                          tools/waits.py             |
 |                                          tools/transact.py          |
 |                                          tools/winproc.py            |
 |                                          tools/system.py             |
 +--------------------------------------------------------------------+
      |
      v
 Windows UI Automation  (uiautomation + comtypes)
 Notepad | Calc | Explorer | Settings | Chrome/Edge via DOM
```

Three rules hold the design together:

1. **Snapshot is batched.** One `CacheRequest` pulls properties, patterns, and the subtree scope in a single traversal instead of one round-trip per property.
2. **Find returns an id and a path.** The path is `window -> parent -> child`, searched one level at a time. There is no depth-3-from-the-root search.
3. **Act uses patterns, not pixels.** `invoke` calls InvokePattern. It never moves the mouse to a rectangle. The four compat tools (`click`, `move`, `scroll`, `shortcut`) are the deliberate escape hatch and are off by default in the recommended allowlist.

### Tools

Seven groups. The spec targets about 35 tools; the plan's task contracts spell each one out by name, so the table below runs longer because a few spec entries split into modes (`clipboard` becomes `clipboard_get`/`clipboard_set`, and the window ops list separately). `server/registry.py` is the source of truth at runtime.

| Group | Tools |
| --- | --- |
| Tree | `list_windows`, `snapshot`, `find`, `dump_tree`, `get_prop`, `get_patterns` |
| Act by pattern | `invoke`, `toggle`, `select`, `expand`, `scroll_into_view`, `focus`, `set_value`, `get_value`, `paste_text`, `menu_click` |
| Compat (opt-in) | `click`, `move`, `scroll`, `shortcut`, `clipboard_get`, `clipboard_set` |
| Waits, one call | `wait`, `wait_text`, `wait_focus`, `wait_enabled`, `wait_gone` |
| Window / process | `app_launch`, `win_move`, `win_resize`, `win_min`, `win_max`, `win_snap`, `get_hwnd_pid_path`, `proc_tree`, `is_elevated`, `integrity_level`, `exclude_proc`, `kill_proc` |
| System | `powershell`, `fs_list`, `fs_read`, `fs_write`, `registry_get`, `notify`, `scrape_dom` |
| Transaction | `preview`, `dry_run`, `before_after`, `profile` |

Target apps: Notepad, Calc, Explorer, Settings, and Chrome/Edge through the DOM. Browser automation goes through `scrape_dom` and the DOM only. Clicking a web page with desktop coordinates is not supported on purpose.

## 3. Demo (90 seconds, terminal only)

Full script with exact commands and expected outputs: [`demo/script.md`](demo/script.md). No screenshots, no GUI recording. The whole demo is a terminal: tests, the eval runner, and the audit log.

## 4. Eval

Golden set of 10 to 15 tasks in `eval/tasks.json`, driven by `eval/run.py`, gated in CI by `.github/workflows/eval-gate.yml` (a PR fails if the success rate drops). The runner reports `ok`, `ms`, and `tokens_est` per task. Task list comes from `eval/tasks.json`; rows below are grouped by target app.

| Target | Task shape | ok | p50 (ms) | p95 (ms) | tokens_est | images |
| --- | --- | --- | --- | --- | --- | --- |
| Notepad | open, type, save via menu | PENDING-RUN | PENDING-RUN | PENDING-RUN | PENDING-RUN | 0 |
| Calc | read the value out of the display | PENDING-RUN | PENDING-RUN | PENDING-RUN | PENDING-RUN | 0 |
| Explorer | find a file by name and open it | PENDING-RUN | PENDING-RUN | PENDING-RUN | PENDING-RUN | 0 |
| Settings | toggle a switch found by name | PENDING-RUN | PENDING-RUN | PENDING-RUN | PENDING-RUN | 0 |
| Chrome (DOM) | read text from a loaded page | PENDING-RUN | PENDING-RUN | PENDING-RUN | PENDING-RUN | 0 |
| **Overall** | N runs over the golden set | PENDING-RUN | PENDING-RUN | PENDING-RUN | PENDING-RUN | 0 |

Reproduce with:

```
python eval/run.py
```

Context, not our result. The published baselines are brutal for everyone: Navi scored 19.5% on WindowsAgentArena against 74.5% for a human, and the best visual grounding models sit near 40% on ScreenSpot-Pro. Those numbers come from the papers, not from this repo. This project does not compete on grounding accuracy; it removes the image from the loop entirely and accepts the narrower target set that comes with that.

## 5. Cost and latency

What gets cheaper, and why, in three moves:

- **One snapshot instead of many reads.** Caching pulls the whole relevant subtree in one traversal. A vision agent needs a fresh full-desktop image per step.
- **No coordinate retry loop.** A pattern call either works or returns a typed error. A pixel click that misses gets retried, and every retry is another image.
- **Text in, text out.** The tree is JSON, so `tokens_est` is measured per call by the eval runner instead of estimated from a screenshot.

`profile` reports timing per stage and the estimated tokens saved, which is the number to read when you want to know where the milliseconds actually went.

Real figures: PENDING-RUN. The stated goal is zero images per task and roughly an order of magnitude below vision cost, measured by `tokens_est` across the golden set.

## 6. Failure modes

Each of these has a defined behavior, and the ones named in the plan's review focus carry a test in `tests/test_failures.py`. The behavior is the contract, not a nice-to-have.

| What goes wrong | What the server does |
| --- | --- |
| App has no UIA provider (Canvas, custom render, game) | `NOT_FOUND` with a reason and a pointer to the vision fallback. Never a coordinate guess. |
| A process makes tree traversal crawl | `exclude_proc` plus a per-call timeout. Slow provider is skipped, not hung on. |
| The same action is delivered twice | Idempotency key on `(window, element, action)`. Exactly one effect. |
| The target window is elevated and the server is not | `ELEVATION_REQUIRED`, typed, with the fix. Never a silent failure. |
| A retry after a form submit | `before_after` log proves exactly-once, so a retry cannot double-submit. |
| An element renders with no name at all | `NOT_FOUND` with the reason and the vision-fallback suggestion. |
| Anything writes to stdout | Banned by design. Logs go to stderr, because stdout is the protocol channel and one stray print kills the session. |

## 7. Install from zero

Needs Windows 10 or 11, Python 3.11 or newer, and git. Nothing else.

```
# 1. Get the code
git clone <repo-url> win-mcp-pro
cd win-mcp-pro

# 2. Isolated environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install the package and its dependencies
pip install -e .

# 4. Smoke test: the package imports
python -c "import server"

# 5. Run the suite
python -m pytest tests -q

# 6. Start the server (stays in the foreground and speaks stdio)
python -m server.main
```

Then point an MCP client at it. Use the virtualenv interpreter, not a bare `python`, or the client will start a server with no dependencies installed:

```json
{
  "mcpServers": {
    "win-mcp-pro": {
      "command": "C:\\path\\to\\win-mcp-pro\\.venv\\Scripts\\python.exe",
      "args": ["-m", "server.main", "--exclude-tools", "click,move,scroll,shortcut"],
      "cwd": "C:\\path\\to\\win-mcp-pro"
    }
  }
}
```

### Repo layout

```
win-mcp-pro/
  server/    main.py  registry.py  audit.py  security.py
  tools/     snapshot  query  patterns  values  compat
             waits  winproc  system  transact
  tests/     unit tests + the failure museum
  eval/      tasks.json  run.py
  docs/      SOP.md
  demo/      script.md
```

### Run as admin when you need to

UIA enumerates more when the process is elevated, and an unelevated server cannot send input to an elevated window (UIPI). Read-only inspection works either way. Start the terminal as Administrator, then run the server from it. Do not run elevated by habit; run elevated when a target window is elevated.

## 8. Tradeoffs

**The tree is a narrower world than the screen.** Anything without a UIA provider is invisible: Canvas, WebGL, games, some Electron builds. That is the price of not paying for images. When the tree cannot see it, the honest answer is `NOT_FOUND`, not a guess.

**stdio keeps v1 simple and single-client.** No network surface, no auth to get wrong, one client at a time. Remote use needs Streamable HTTP with a bearer token, an IP allowlist, TLS, and OAuth PKCE. That is documented as an extension, not shipped here.

**Elevation is a real decision.** More enumeration and more reach, but running as admin is a security posture, not a config flag. The default is unelevated, and `ELEVATION_REQUIRED` tells you when to change that.

**Four compat tools are a loaded gun.** `click`, `move`, `scroll`, and `shortcut` break every rule above, on purpose, for the apps the tree cannot drive. They are the first things to switch off with `--exclude-tools`.

**The audit log is not free.** Every call writes a JSONL row to stderr. It is cheap, and it is the only reason you can answer "what did the agent do to my machine" after the fact. Turn it off and you lose that answer.

**Text selection through the tree does not work.** This is an inherited UIA limitation, not a bug to fix here. The same goes for typing into an IDE character by character: use `set_value` or `paste_text`.

**Not all 35 tools are safe.** `kill_proc`, `fs_write`, and `registry_get` touch the machine. Keep the allowlist tight, and make `preview` mandatory before anything irreversible.
