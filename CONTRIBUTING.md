# Contributing

Small, readable and provable beats big. A change is welcome when it keeps the
server easy to audit.

## Setup

```powershell
git clone https://github.com/danzaio/win-mcp-pro.git
cd win-mcp-pro
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check win_mcp_pro tests bench
```

Use the venv interpreter directly; there is no need to run `Activate.ps1`.

## Rules

1. **A new tool must earn its place.** It needs a reason that `find`, `invoke`,
   `set_value` or `click` cannot already cover.
2. **Every behaviour has a test, and a bug fix has a test that fails without
   the fix.** Tests run against fakes, so no unit test touches your desktop.
3. **A refusal sends nothing.** If a check fails, return a typed error before
   any input event, and prove it with the SendInput spy in
   `tests/test_input_binding.py`.
4. **Do not print to stdout.** stdout is the MCP protocol. Log to stderr.
5. **No dead code, no speculative flags.** If nothing calls it, delete it.

## Adding a tool

Create `win_mcp_pro/tools/<group>.py` that exposes `TOOLS`, a list of dicts
with `name`, `description`, `schema` (a pydantic model class) and `handler`.
`tools.load_all_modules()` finds the module by file name. If the tool is
read-only, add it to `READ_ONLY_TOOLS` in `win_mcp_pro/server/security.py`.

## Live checks

`bench/run_matrix.py` drives disposable fixtures with a real desktop and moves
your cursor. Run it on an idle machine. It is not part of CI.
