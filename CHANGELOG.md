# Changelog

## 1.0.0

First tagged release. `0.x` was a private working tree.

### Changed
- Packaged as one `win_mcp_pro` package with a `win-mcp-pro` command, so
  `uvx win-mcp-pro` works. The old top-level `server` and `tools` packages are
  gone; they would have collided in `site-packages`.
- Windows-only dependencies carry `sys_platform == 'win32'` markers.
- Cut from 83 tools to 54 and from 10.3k to 7.6k lines. Removed: the browser
  and CDP tools, `powershell`, `fs_*`, `registry_get`, `notify`, `env_get`,
  `scrape_dom`, `img_convert`, `code_goto`, `win_topmost`, `color_at`,
  `idle_time`, `proc_tree`, `get_hwnd_pid_path`, `is_elevated`,
  `integrity_level`, `kill_proc`, and the unwired `preview` / `dry_run` /
  `before_after` / `profile` module. The last tagged state with all of them is
  `v0.1-full`.
- The fake-client `eval/` suite was removed: it passed by construction.

### Added
- `--read-only`: inspection and waiting tools only.
- `win_mcp_pro.__version__`, reported in the MCP handshake.
- CI on Windows (lint, tests on Python 3.11 to 3.13, build and run the wheel
  in a clean environment) and tag-driven PyPI publishing. The publish job
  stays skipped until the `PUBLISH_PYPI` repository variable is `true`.
- A README with diagrams and a looping demo, issue and PR templates, and
  Dependabot for pip and GitHub Actions.

### Fixed
- `replay` called tool handlers with the wrong calling convention and failed
  for every keyword-only tool (`set_value`, `invoke`, `snapshot`, ...). All
  dispatch now goes through one helper.
- `winproc` re-opened `user32` and discarded its own Win32 prototypes, so
  64-bit handles were truncated to a C int. Prototypes are now declared once.
- `win_snap` used the primary monitor's work area. It now uses the monitor the
  window is on.
- `menu_click` walked the first window in Z-order instead of the foreground one.
- `patterns` handlers broke when a client sent `element_id` by keyword.
- `DRY_RUN=true` disabled input but not screen capture or OCR.
