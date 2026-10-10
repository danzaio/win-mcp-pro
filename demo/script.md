# Demo script — win-mcp-pro (90 seconds, terminal only)

No screenshots, no GUI recording, no mouse. Everything below is a terminal. Record with asciinema or a plain terminal capture.

**Before recording:** run every command once for real and paste the actual output into the `<PLACEHOLDER>` slots. Never record a number you did not get from your own machine. `eval/run.py` output, pytest counts, and timings all have to come from a live run.

Runtime budget: 90 seconds total. If the eval runner takes longer on your box, cut it down (see note at the bottom).

---

## 0:00 — 0:08 · Setup and hook

Open a terminal in the repo with the venv active.

```
> python -c "import server"
```

Expected: no output, exit 0. That is the point. The package imports clean with UIA and comtypes loaded.

Say: "This is a Windows computer-use MCP server. It reads the UI as a tree, not as an image. Ninety seconds, all terminal."

## 0:08 — 0:22 · The suite is green

```
> python -m pytest tests -q
```

Expected (fill from your run):

```
..............................................  [100%]
<N> passed in <T>s
```

## 0:22 — 0:36 · The failure museum

This is the part that matters. These tests are the reasons the server does not hang, does not double-fire, and does not click blind.

```
> python -m pytest tests/test_failures.py -q
```

Expected (fill from your run):

```
.................  [100%]
<N> passed in <T>s
```

Point at the cases while it runs: duplicate delivery fires once, a slow provider is skipped instead of hung, an elevated target returns a typed error, a retry after submit cannot double-submit.

## 0:36 — 1:05 · The eval run

Golden set, real apps, real timings.

```
> python eval/run.py 2>audit.jsonl
```

Expected: a table, one row per task, with `ok`, `ms`, and `tokens_est` per row. Fill every number from the run you just did.

```
task                     ok      ms     tokens_est
notepad_open_type_save   <bool>  <ms>   <tokens>
calc_read_display        <bool>  <ms>   <tokens>
explorer_find_open       <bool>  <ms>   <tokens>
settings_toggle_by_name  <bool>  <ms>   <tokens>
chrome_dom_read_text     <bool>  <ms>   <tokens>
...
```

Note the `2>audit.jsonl`: the audit log is being captured to a file while stdout stays clean for the table.

## 1:05 — 1:18 · Every call leaves a row

```
> Get-Content audit.jsonl -Tail 3
```

Expected: three JSONL rows. Field names follow `server/audit.py`; paste the real ones from your run.

```
{"ts":"<timestamp>", "tool":"invoke", "target":"<window/element>", "result":"ok", "ms":<n>}
{"ts":"<timestamp>", "tool":"set_value", "target":"<window/element>", "result":"ok", "ms":<n>}
{"ts":"<timestamp>", "tool":"menu_click", "path":["File","Save"], "result":"ok", "ms":<n>}
```

Say: "Every call has a row. Who did what, to which element, and how long it took."

## 1:18 — 1:30 · Close

No more commands. Leave the eval table and the last audit rows on screen and say three lines:

- Tree-first, so no image tokens on the happy path.
- Stable ids and Control Patterns, so no coordinate drift and a real audit trail.
- Every number above came from this machine, and the CI gate fails a PR if they drop.

Then stop the recording.

---

## Cutting it down

The runner prints every task in `eval/tasks.json`. If the full set blows the 90-second budget, run a subset for the recording and say so out loud. Do not trim the table after the fact to make it look faster. The honest version is the version that gets the repo taken seriously.

## Recording notes

- Terminal window wide enough that the eval table does not wrap. Wrapped tables are unreadable in a GIF.
- Font size one step larger than normal. It will be watched at small size.
- No background noise from other apps during the eval run; app launches are audible.
- Keep the cursor still. A jumping cursor makes the recording feel edited.
