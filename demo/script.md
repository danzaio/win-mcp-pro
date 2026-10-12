# Demo script — win-mcp-pro (90 seconds, terminal only)

No screenshots, no GUI recording, no mouse. Everything below runs in a terminal. Record with asciinema or a plain terminal capture.

**Before recording:** run every command once and paste the actual output into the `<PLACEHOLDER>` slots. Never record a pytest count you did not get from your own machine. Eval timings and rows come from the fake client; they are not desktop measurements.

**Two gates before you record.** `python -m pytest tests -q` must be green, and `python -m pytest tests/test_failures.py -q` must be green. If either is red, the slice behind it is still landing. Say that out loud in the recording or fix it first. Do not edit output to look clean.

Prereqs: venv built (`.venv\Scripts\python -m pip install -e ".[dev]"`), terminal already inside the repo.

---

## 0:00 — 0:08 · It imports

```
> .venv\Scripts\python -c "import server"
```

Expected: no output, exit 0. This checks the package import only; it does not prove UI Automation works against a live app.

Say: "This is a Windows computer-use MCP server. It reads the UI as a tree, not as an image. Ninety seconds, all terminal."

## 0:08 — 0:20 · The suite

```
> .venv\Scripts\python -m pytest tests -q
```

Expected (fill from your run):

```
.......................  [100%]
<N> passed in <T>s
```

Any collection error is a build blocker in this checkout. Do not skip the affected tool group.

## 0:20 — 0:36 · The failure museum

This is the failure-museum suite. Its test doubles pin selected helper contracts; it does not prove live desktop behavior or human approval.

```
> .venv\Scripts\python -m pytest tests/test_failures.py -q
```

Expected (fill from your run):

```
........  [100%]
<N> passed in <T>s
```

Describe only the cases shown in the current output. The duplicate-delivery scenario exercises the transaction helper, not the direct MCP pattern/value actions.

## 0:36 — 0:56 · The golden set, and what the numbers are not

```
> .venv\Scripts\python eval\run.py --out "$env:TEMP\win-mcp-pro-eval-results.json" 2>$null
```

Expected: 12 rows, one per task, with `ok`, `ms`, `tokens_est`, and a reason column. Every row passes with the fake client, by design, because the answers are canned. Fill every value from the run you just did.

```
| # | task | app | ok | ms | tokens_est | why |
| --: | --- | --- | :---: | --: | --: | --- |
| 1 | notepad-01-launch-and-list | notepad | PASS | <ms> | <t> | found 'Notepad' |
...
| 12 | chrome-dom-02-locate-submit-button | chrome-dom | PASS | <ms> | <t> | matched /id="login-submit"/ |
| | **12/12** | | | | | rate 1.00 |
```

**Say this out loud, it is the honest beat:** "That ran against a fake client. The runner, the checks, and the table are real, the answers are canned. It proves eval plumbing. A separate smoke has exercised the live MCP server only against our disposable test form; the 12-task live evaluation has not run."

The CI threshold is applied to the same fake-client result. It does not gate a live desktop success rate.

## 0:56 — 1:12 · Stdio and call metadata

Start the server with the read-only tool allowlist. It sits in the foreground and speaks stdio; startup and call metadata go to stderr.

```
> .venv\Scripts\python -m server.main --tools list_windows,snapshot,find,dump_tree,get_prop,get_patterns
```

Expected, on stderr, one JSON line:

```
{"ts": "<timestamp>", "event": "server_start", "allowlist": ["dump_tree", "find", "get_patterns", "get_prop", "list_windows", "snapshot"], "blocklist": null, "tools": 53}
```

Point out the allowlist and that stdout remains reserved for JSON-RPC. The log contains tool and field names, not argument values or proof of effects.

Ctrl-C to end the pane.

## 1:12 — 1:30 · Close

No more commands. Leave the failure museum result and the eval table on screen and say three lines:

- Tree-first, so no image tokens on the happy path.
- Stable ids and Control Patterns, with transient stderr call metadata rather than a durable audit trail.
- The eval run and CI gate use canned responses; live desktop performance is unmeasured.

Then stop the recording.

---

## Cutting it down

`python eval/run.py --out "$env:TEMP\win-mcp-pro-eval-results.json"` prints all 12 tasks. If that overruns the budget, trim the recording and say the run was cut. Do not edit the table afterwards.

## Recording notes

- Terminal wide enough that the eval table does not wrap. A wrapped table is unreadable in a GIF.
- Font one step larger than normal. It will be watched at small size.
- Silence notifications before the eval beat; app launches are audible.
- Keep the cursor still. A jumping cursor makes the recording look edited.
- The eval command writes its output to the system temp directory so it does not overwrite a local `eval/results.json`.
