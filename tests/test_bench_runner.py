"""Unit tests for the bench runner's pure helpers (no fixtures, no server)."""

from bench import run_matrix as runner


def test_read_events_parses_jsonl_and_tolerates_noise(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"event": "text", "value": "x"}\n\nnot-json\n{"event": "button1"}\n',
        encoding="utf-8",
    )
    events = runner._read_events(str(path))
    assert [event["event"] for event in events] == ["text", "button1"]


def test_read_events_missing_file_is_empty(tmp_path):
    assert runner._read_events(str(tmp_path / "nope.jsonl")) == []


def test_count_and_summarize():
    events = [{"event": "mouse"}, {"event": "button1"}, {"event": "mouse"}]
    assert runner._count(events, "mouse") == 2
    results = [
        {"id": "T1", "name": "a", "ok": True, "skipped": False, "ms": 10.0,
         "calls": 2, "detail": ""},
        {"id": "T2", "name": "b", "ok": False, "skipped": False, "ms": 5.0,
         "calls": 1, "detail": "x"},
        {"id": "T3", "name": "c", "ok": True, "skipped": True, "ms": 1.0,
         "calls": 0, "detail": ""},
    ]
    summary = runner._summarize(results)
    assert summary == {"tasks": 3, "passed": 1, "failed": 1, "skipped": 1,
                       "calls": 3, "ms": 16.0}
