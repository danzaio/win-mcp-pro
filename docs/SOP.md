# SOP — win-mcp-pro (handoff)

One page. Install it, run it, keep it alive. Owner: whoever is on call for the automation box.

## Install

Needs Windows 10/11, Python 3.11+, git. Nothing else.

```
git clone <repo-url> win-mcp-pro
cd win-mcp-pro
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -c "import server"     # smoke test, must exit 0
.venv\Scripts\python -m pytest tests -q     # suite
```

Use the venv interpreter directly instead of running `Activate.ps1`. Some machines block that script through execution policy and the failure looks like a broken repo.

If `import server` fails, the venv is incomplete. Redo `pip install -e .` and read the error, it is almost always a missing dependency.

## Run

The server is a foreground stdio process. The MCP client starts it, not you.

```
.venv\Scripts\python -m server.main
```

Flags: `--tools` and `--exclude-tools`, both comma-separated, both also accept the `--tools=a,b` form. Point the client at the **venv interpreter**, never a bare `python`:

```json
{
  "mcpServers": {
    "win-mcp-pro": {
      "command": "C:\\path\\to\\win-mcp-pro\\.venv\\Scripts\\python.exe",
      "args": ["-m", "server.main", "--exclude-tools", "click,move,scroll,shortcut,kill_proc"],
      "cwd": "C:\\path\\to\\win-mcp-pro"
    }
  }
}
```

stdout is the JSON-RPC channel. Every log line and every audit row goes to stderr as JSONL, one event per line. One stray `print` to stdout kills the session, and the symptom is a client that starts the server and then sees it die with no error.

## Allowlist

Two flags, comma-separated: `--tools a,b,c` (only these load) and `--exclude-tools x,y` (everything except these). Both also accept the `--tools=a,b` form. Enforcement lives in `server/security.py:enforce_filter`.

Three rules that hold:

- **Blocklist wins over allowlist.** A tool named in `--exclude-tools` stays hidden even if `--tools` also names it.
- **Omitting a flag means no restriction from it.** With neither flag, every registered tool is exposed.
- **A list with no names in it exposes nothing.** `--tools ,` hides every tool and fails closed.

**Do not pass an empty `--tools=""`.** It looks like "allow nothing" and it is the opposite. `server/main.py` parses flags with its own helper that turns an empty string into "no restriction", so `--tools ""` exposes **every** tool. The parser in `server/security.py:parse_tool_filter` rejects that case and raises, so the two disagree. Until the entrypoint uses the security parser, `--tools ,` is the only safe way to expose nothing. Flag this in review; it is a fail-open, not a docs problem.

Recommended starting posture for a box doing real work:

```
--exclude-tools click,move,scroll,shortcut,kill_proc
```

The four compat tools break the no-coordinate rule and `kill_proc` ends processes. Add them per task, not by default. If a tool is missing from the client, check the allowlist before you debug the server.

## Admin and UIPI

UIA enumerates more when the process is elevated, and an **unelevated server cannot send input to an elevated window** (UIPI). Read-only inspection works either way.

- Normal day: run unelevated.
- Target window is elevated: restart the terminal as Administrator and start the server from it.
- The server never guesses. When integrity levels do not line up it returns `ELEVATION_REQUIRED` with the fix, fast. A hang is a bug, not a policy.

## When to use the vision fallback

Image is opt-in and every use is logged with a reason and a cost. Reach for it only when the tree genuinely cannot see the target:

- Canvas, WebGL, or a custom-rendered surface with no UIA provider.
- An icon or glyph with no accessible name at all.
- A target `find` returns `NOT_FOUND` for, after you confirmed the element exists on screen.

Before you fall back, rule out the boring causes: the app is not in `exclude_proc`, the element really has a provider (check with Inspect.exe from the Windows SDK, or Accessibility Insights), and `find` was given the right name or AutomationId. Most "the tree is blind" reports are a wrong query.

## Support checklist

Work top to bottom. Do not skip.

1. `python -c "import server"` — a failure here is venv or install, never the server.
2. Tools missing from the client — check `--tools` / `--exclude-tools` first, then confirm the tool is actually registered: `.venv\Scripts\python -c "import tools; tools.load_all_modules(); print(sorted(tools.names()))"`. A tool that is not in that list has not landed, and no allowlist will conjure it.
3. Client says the server failed to start — the command is not the venv python, or the cwd is wrong.
4. Session dies right after start — something wrote to stdout. Search the code for a bare `print`.
5. `pytest` shows a collection error naming a tool module — that group has not landed yet. Run the file for a group that has, and report the gap instead of working around it.
6. `find` returns `NOT_FOUND` — verify the app has a provider with Inspect.exe. If it does, the query is wrong.
7. Action returns `ELEVATION_REQUIRED` — elevation mismatch. Restart elevated or pick another target.
8. Snapshot hangs on one app — add it to `exclude_proc` and confirm the per-call timeout fired. Skip, do not hang.
9. An action fired twice — read the stderr audit log for the `(window, element, action)` idempotency key. If the key is there, the retry was absorbed and the double effect came from somewhere else.
10. Form submitted twice — check the `before_after` log. That tool exists to prove exactly-once.
11. Client connects but sees **zero tools** — the registry is empty. `run_server()` in `server/registry.py` never calls `tools.load_all_modules()`, so the entrypoint has to. Run the count command in step 2 to tell the two apart: a `0` with no error is the missing load call, an `ImportError` is a bad import in a landed module.
12. `load_all_modules()` raises `ImportError` — one module has a bad import and the whole call fails, because loading is all-or-nothing. `tools/winproc.py` was doing `from pydantic import ... Literal`, and pydantic 2.14 does not re-export `Literal` from the top level. Fix that line to `from typing import Literal` and the registry loads again.
13. Reproduce against the golden set before changing code: `python eval/run.py`.

## Repo hygiene

- `@AutomationLog.txt` appears in the repo root whenever the UIA COM path runs and cannot initialize (`CoInitialize não foi chamado`). It is not gitignored, and it is noise rather than a symptom of your change. It should be in `.gitignore` before someone commits it.
- `eval/results.json` is generated by the runner. It now sits in `.gitignore`, but it is still tracked from an earlier commit, so it keeps showing up in `git status` until someone runs `git rm --cached eval/results.json`. It holds fake-client output either way: never read it as a measurement.

## Known gaps

- No UIA provider means invisible. By design and documented, not a defect to chase.
- Text selection through the tree is not supported (inherited UIA limitation).
- Type with `set_value` or `paste_text`. There is no character-by-character typing.
- Adding a tool: create `tools/<group>.py` exposing `TOOLS` with `name`, `description`, `schema` (a pydantic model class) and `handler`. `tools.load_all_modules()` finds it by filename. The registry is the single source of truth, in registration order.
- v1 is stdio and single-client. Remote use needs the documented Streamable HTTP extension with auth, TLS, and an IP allowlist.
