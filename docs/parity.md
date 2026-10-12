# Parity and evidence

Where each advertised capability is proven. Tree: 2026-10-12, master
`cf09e27` (with the E-phase additions landing on top).

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
