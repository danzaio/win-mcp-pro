"""Tests for tools.doctor - environment/dependency self-check.

Every check reads through a seam, so the report shape is pinned without a
real UIA/browser/OCR environment.
"""

from __future__ import annotations

import pytest

from tools import doctor


@pytest.fixture()
def all_good(monkeypatch):
    monkeypatch.setattr(doctor, "_module_ok", lambda name: (True, "imported"))
    monkeypatch.setattr(doctor, "_browser_path", lambda: r"C:\Edge\msedge.exe")
    monkeypatch.setattr(doctor, "_ocr_languages", lambda: ["en-US", "pt-BR"])
    monkeypatch.setattr(doctor, "_uia_reachable", lambda: (True, "root control ok"))
    return None


def test_doctor_reports_every_check(all_good):
    result = doctor.handler_doctor({})
    assert result["ok"] is True, result
    names = [check["name"] for check in result["checks"]]
    for expected in ("python", "module:uiautomation", "module:mss", "module:PIL",
                     "module:winsdk", "module:websocket", "browser", "ocr_languages",
                     "uia"):
        assert expected in names, names
    assert result["summary"].endswith("ok")


def test_doctor_marks_failures_and_overall_not_ok(monkeypatch):
    monkeypatch.setattr(doctor, "_module_ok",
                        lambda name: (name != "winsdk", "boom" if name == "winsdk" else "ok"))
    monkeypatch.setattr(doctor, "_browser_path", lambda: None)
    monkeypatch.setattr(doctor, "_ocr_languages", lambda: [])
    monkeypatch.setattr(doctor, "_uia_reachable", lambda: (False, "no root"))
    result = doctor.handler_doctor({})
    assert result["ok"] is False
    failed = {check["name"] for check in result["checks"] if not check["ok"]}
    assert {"module:winsdk", "browser", "ocr_languages", "uia"} <= failed


def test_doctor_tool_surface():
    names = {t["name"] for t in doctor.TOOLS}
    assert names == {"doctor"}
    for tool in doctor.TOOLS:
        assert tool["description"].strip()
        assert callable(tool["handler"])
