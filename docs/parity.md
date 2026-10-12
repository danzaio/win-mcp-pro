# Parity and evidence

Where each advertised capability is proven. Tree: 2026-10-12, master
`c827d4d`.

## Reference parity (OMP / Microsoft computer use)

What the reference implementations do (from the pinned research) and where
our version of it lives:

| Capability observed in references | Our implementation | Proven by |
| --- | --- | --- |
| Physical-frame pinning: coordinates belong to a specific capture (OMP frames, Cua capture tokens) | frame ids with TTL + target verification before any input event | `tests/test_input_binding.py`, `tests/test_vision.py`; commit `a3a2490` |
| Ref lifecycle with typed stale errors (OMP refs, Cua tokens) | identity contract for UIA ids; `STALE_REF`/`UNKNOWN_REF` for browser refs | `tests/test_identity_contract.py`, `tests/test_browser.py`; commits `e4dc147`, `bafbb09` |
| Refusal taxonomy per toolkit (OMP) | 12 typed input refusal codes, `WORKER_*` worker codes, browser `*_REF` codes | `tests/test_input_binding.py`, `tests/test_uia_worker.py`, `tests/test_browser.py` |
| Explicit takeover/restore (OMP saves/restores foreground and cursor) | bounded activation attempt with `ACTIVATION_FAILED`; the live click test restores the cursor | `tests/test_input_binding.py`, `tests/test_live_click_binding.py` |
| Short-send cleanup, no partial resend (OMP) | one SendInput batch per action; refusals happen before any event is sent | `tests/test_input_binding.py` (zero-input spy) |
| Supervised worker with reset semantics (OMP worker + supervisor) | `server/supervisor.py` + `server/worker.py` (deadline, kill, respawn, crash-loop guard, no double retry) | `tests/test_uia_worker.py`; commit `951d5ff` |
| DOM automation (Microsoft Windows 365 for Agents) | isolated `browser_*` tools over CDP (disposable profile, no OS input) | `tests/test_browser.py`, `tests/test_live_browser.py`; commit `bafbb09` |
| OCR (Microsoft Windows 365 for Agents) | `ocr_read` with per-word boxes and a clickable frame | `tests/test_ocr.py`, `tests/test_live_ocr.py`; commit `7ee9333` |
| Screenshots as protocol images (Microsoft, Playwright MCP) | `ImageContent` envelope with origin/frame metadata and a 4 MB cap | `tests/test_vision.py`; commit `44a51ee` |
| Sandboxed execution (Microsoft cloud PC) | **not claimed**: local process. Isolation here is the disposable browser profile plus tool filters, not an OS sandbox | see "Not claimed" |

## Capability evidence

| Capability | Proven by |
| --- | --- |
| Element identity: unique ids, same-name siblings distinct, waits observe the named child, focus compares identity | `tests/test_identity_contract.py`; commits `e4dc147` |
| Frame-pinned input: target+frame verified before any event, typed refusals, zero input on refusal | `tests/test_input_binding.py`; opt-in `tests/test_live_click_binding.py`; commit `a3a2490` |
| Multi-monitor absolute input (`MOUSEEVENTF_VIRTUALDESK`) | `tests/test_input_binding.py::test_absolute_move_spans_the_virtual_desktop`; live click test; commit `a3a2490` |
| Screenshots as MCP image content + frame metadata + origin | `tests/test_vision.py`; commit `44a51ee` |
| Supervised worker for tree walks (deadline, kill, respawn, crash-loop guard, no double retry) | `tests/test_uia_worker.py`; commit `951d5ff` |
| Isolated browser DOM over CDP (refs, stale/unknown refusal, no OS input, disposable profile) | `tests/test_browser.py`; opt-in `tests/test_live_browser.py`; commit `bafbb09` |
| OCR read with per-word boxes and a clickable frame | `tests/test_ocr.py`; opt-in `tests/test_live_ocr.py`; commit `7ee9333` |
| Real-library API correctness (waits/values/patterns/ocr fixed against actual uiautomation shapes) | `tests/test_waits.py`, `tests/test_values.py`, `tests/test_patterns.py`, `tests/test_ocr.py`; commit `5417a1d` |
| Driver-conformance matrix with an external oracle and zero wrong-target leakage | `bench/run_matrix.py`; `bench/results/bench-20261012-164216.md`; commit `cf09e27` |
| Replay with drift/ambiguity refusal | `tests/test_replay.py`; bench task T12 |
| Environment doctor (deps, browser, OCR languages, UIA) | `tests/test_doctor.py`; bench task T13 |
| Packaging: clean wheel, `pip check`, stdio smokes, CI | wheel smoke evidence in this session; CI run `38299852835` |

## Audit note

Audited 2026-10-12 against this tree: the README tool count (63 across 14
modules) was counted from the live registry (`tools.load_all_modules()`);
test counts come from the suite run recorded in the README; every CI run
listed here was checked with `gh run view`. The README was re-read line by
line and its wording corrected where the scope grew (browser/OCR/replay
sections, live-test pluralization).

## Not claimed

- No head-to-head benchmark against other MCP servers: competitors were
  researched from pinned sources, never run. A comparison needs pinned
  versions, license review and an explicit budget decision.
- No LLM task-success benchmark; the matrix is driver conformance.
- Fixture coverage is WinForms + Tk; WPF, WebView2 and canvas fixtures are
  roadmap items.
- Filters (`--tools`, `--exclude-tools`) are policy, not an OS sandbox.
- Cooperative timeouts cannot interrupt an in-flight synchronous COM call;
  that is exactly why the optional worker mode exists.
