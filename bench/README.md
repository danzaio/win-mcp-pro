# bench - driver-conformance matrix

```powershell
.venv\Scripts\python.exe bench\run_matrix.py
```

Spawns disposable fixtures and the MCP server over stdio, then runs 28
deterministic tasks. Every observable effect is written by the fixture
process itself to a JSONL log; the runner asserts against those logs (the
external oracle), so a green run means the effect happened in the
application, not just that the server replied `ok`. A decoy fixture's log
proves zero wrong-target leakage.

Fixtures:

- **WinForms target** (PowerShell): textbox, two buttons, status label; logs
  text changes, button clicks, mouse down/up and raw mouse downs.
- **WinForms decoy**: same app, different window/pid; its log must stay
  empty - it is the wrong-target trap.
- **Tk fixture** (Python): a toolkit with no useful UIA tree; logs its own
  button center in screen coordinates, so the coordinate path can be
  exercised where the accessibility tree has nothing to offer.

Tasks: identity find, set_value + readback, invoke, wait_text,
wait_enabled, frame-pinned click, unknown-frame refusal, wrong-target
refusal + leak check, dump_tree, ocr_read, Tk coordinate click, replay
with id binding, doctor, cursor_position, double_click, ocr_click,
wait_change, displays, form_fill, table_extract, choose_file (a real Open
dialog in Notepad), win_close, get_active_window, open_default (including a
refused missing path), type_text, key_hold, a stepped drag, win_shot.

Run it on an idle desktop: it moves the real cursor and asserts effects on
its own windows; concurrent physical mouse activity can disrupt a synthetic
click.

Results land in `bench/results/bench-<stamp>.{json,md}`; the exit code is
non-zero if any task fails. `--only T01,T06` runs a subset while debugging.

## What it caught

The first full run (2026-10-12) failed 6/11 and exposed four real product
bugs, each fixed with a regression test:

- waits read `GetName()`/`GetEnabled()` methods the real uiautomation
  library does not have (`Name`/`IsEnabled` are properties), so every
  `wait_text`/`wait_enabled` against a real control timed out;
- the pattern resolver matched the runtime id with the window handle
  prefixed, so snapshot-derived ids never resolved through its walk;
- `values` read a `RuntimeId` attribute that does not exist (only
  `GetRuntimeId()` does);
- `ocr_read` called `asyncio.run` inside the server's event loop.

## Honest scope

This measures driver conformance on the machine that runs it. It is not an
LLM task-success benchmark, not a comparison against other MCP servers, and
not a claim that every Windows application was tested. The fixture set
covers WinForms and Tk today; WPF, WebView2 and canvas fixtures are
roadmap items. Competitor runs would need pinned versions, licenses and an
explicit budget decision before any comparison is published.
