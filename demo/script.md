# Demo script — win-mcp-pro (90 seconds, terminal only)

No screenshots, no GUI recording, no mouse. Everything below runs in a terminal. Record with asciinema or a plain terminal capture.

**Before recording:** run every command once for real and paste the actual output into the `<PLACEHOLDER>` slots. Never record a number you did not get from your own machine. Pytest counts, timings, and the eval table all have to come from a live run.

**Two gates before you record.** `python -m pytest tests -q` must be green, and `python -m pytest tests/test_failures.py -q` must be green. If either is red, the slice behind it is still landing. Say that out loud in the recording or fix it first. Do not edit output to look clean.

Prereqs: venv built (`.venv\Scripts\python -m pip install -e .`), terminal already inside the repo.

---

## 0:00 — 0:08 · It imports

```
> .venv\Scripts\python -c "import server"
```

Expected: no output, exit 0. That silence is the point: `uiautomation` and `comtypes` both loaded and the package is intact.

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

`ERROR collecting tests/test_<group>.py` means that tool group has not landed yet. That is a real gap in the build, not a broken environment. Report it, do not route around it.

## 0:20 — 0:36 · The failure museum

This is the part that matters. These tests are the reasons the server skips instead of hanging, fires once instead of twice, and fails typed instead of dying quietly.

```
> .venv\Scripts\python -m pytest tests/test_failures.py -q
```

Expected (fill from your run):

```
........  [100%]
8 passed in <T>s
```

Name the cases as they run: slow provider skipped when excluded and timed out per call, duplicate delivery producing a single effect, an elevated target returning a typed error, a double submit happening exactly once, and a missing element coming back as `NOT_FOUND` with a vision hint.

## 0:36 — 0:56 · The golden set, and what the numbers are not

```
> .venv\Scripts\python eval\run.py 2>eval-stderr.txt
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

**Say this out loud, it is the honest beat:** "That ran against a fake client. The runner, the checks, and the table are real, the answers are canned. It proves the plumbing. The numbers that count come from driving the live server against a real desktop, and that run has not happened yet."

Then the gate: `--min-success 0.8` is what CI runs, so a PR that drops the success rate below the floor fails the build.

## 0:56 — 1:12 · Every call leaves a row

Start the server in this pane. It sits in the foreground and speaks stdio, and the audit row appears on stderr the moment it starts.

```
> .venv\Scripts\python -m server.main --exclude-tools click,move,scroll,shortcut
```

Expected, on stderr, one JSON line:

```
{"ts": "<timestamp>", "event": "server_start", "allowlist": null, "blocklist": ["click", "move", "scroll", "shortcut"]}
```

Point at two things: the row is on stderr, and it names the blocklist it was started with. Then say: "Every tool call adds a row the same way. stdout stays clean because stdout is the protocol channel. One stray print and the session dies."

Ctrl-C to end the pane.

## 1:12 — 1:30 · Close

No more commands. Leave the failure museum result and the eval table on screen and say three lines:

- Tree-first, so no image tokens on the happy path.
- Stable ids and Control Patterns, so no coordinate drift and a real audit trail.
- The eval run is canned on purpose, and the CI gate fails a PR if the real numbers drop.

Then stop the recording.

---

## Cutting it down

`python eval/run.py` prints all 12 tasks. If that overruns the budget, trim the recording and say the run was cut. Do not edit the table afterwards. The honest version is the one that gets the repo taken seriously.

## Recording notes

- Terminal wide enough that the eval table does not wrap. A wrapped table is unreadable in a GIF.
- Font one step larger than normal. It will be watched at small size.
- Silence notifications before the eval beat; app launches are audible.
- Keep the cursor still. A jumping cursor makes the recording look edited.
- `2>eval-stderr.txt` keeps any stderr noise out of the table output. Remove the file before recording if you want a clean pane, or `type eval-stderr.txt` at the end to show the JSONL rows.
