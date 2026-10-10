"""Tests for the system tools: powershell, fs_*, registry_get, notify, scrape_dom.

Self-bootstraps sys.path because this task's slice is only tools/system.py and
tests/test_system.py (the package __init__ files belong to Task 1).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import system as sysmod  # noqa: E402

IS_WIN = sys.platform == "win32"
needs_win = pytest.mark.skipif(not IS_WIN, reason="Windows-only tool")


def _ps_available() -> bool:
    if not IS_WIN:
        return False
    import shutil

    return shutil.which("powershell") is not None


def call(tool_name: str, **kwargs: Any) -> dict:
    """Invoke a registered tool the way the server would (schema then handler)."""
    tool = next(t for t in sysmod.TOOLS if t["name"] == tool_name)
    args = tool["schema"](**kwargs)
    return tool["handler"](args)


# ---------------------------------------------------------------- registry shape


def test_tools_list_shape() -> None:
    assert [t["name"] for t in sysmod.TOOLS] == [
        "powershell",
        "fs_list",
        "fs_read",
        "fs_write",
        "registry_get",
        "notify",
        "scrape_dom",
    ]
    for tool in sysmod.TOOLS:
        assert set(tool.keys()) == {"name", "description", "schema", "handler"}
        assert tool["description"]
        assert issubclass(tool["schema"], BaseModel)
        assert callable(tool["handler"])
        # The registry (Task 2) needs JSON Schema out of every tool.
        assert isinstance(tool["schema"].model_json_schema(), dict)


def test_schemas_validate_input() -> None:
    with pytest.raises(ValidationError):
        sysmod.PowerShellArgs(cmd="")
    with pytest.raises(ValidationError):
        sysmod.PowerShellArgs(cmd="echo hi", timeout_ms=0)
    assert sysmod.PowerShellArgs(cmd="echo hi").timeout_ms == 10_000
    # write needs an explicit confirm; the default must stay off
    assert sysmod.FsWriteArgs(path="a.txt", content="x").confirm is False


# ------------------------------------------------------------------- powershell


@pytest.mark.skipif(not _ps_available(), reason="powershell.exe not available")
def test_powershell_echo() -> None:
    res = call("powershell", cmd="echo hello")
    assert res["ok"] is True
    assert res["exit_code"] == 0
    assert res["timed_out"] is False
    assert "hello" in res["stdout"]
    assert res["cmd_len"] == len("echo hello")
    assert isinstance(res["elapsed_ms"], int)


@pytest.mark.skipif(not _ps_available(), reason="powershell.exe not available")
def test_powershell_captures_stderr_and_nonzero_exit() -> None:
    res = call("powershell", cmd="Write-Error boom")
    assert res["ok"] is False  # it ran, but the exit code is nonzero
    assert res["exit_code"] != 0
    assert "boom" in res["stderr"]


def _pid_alive(pid: int) -> bool:
    """True while the process still runs (STILL_ACTIVE). Windows-only."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@pytest.mark.skipif(not _ps_available(), reason="powershell.exe not available")
def test_powershell_timeout_kills_process() -> None:
    res = call("powershell", cmd="Start-Sleep -Seconds 120", timeout_ms=1500)
    assert res["ok"] is False
    assert res["error"] == "TIMEOUT"
    assert res["timed_out"] is True
    assert res["exit_code"] is None
    assert res["elapsed_ms"] < 15_000  # came back fast, did not wait out the sleep

    # the budget is only honest if the process is gone: poll for its death
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pid_alive(res["pid"]):
        time.sleep(0.1)
    assert not _pid_alive(res["pid"]), "timed-out powershell was left running"


@pytest.mark.skipif(not _ps_available(), reason="powershell.exe not available")
def test_powershell_truncates_output() -> None:
    res = call("powershell", cmd="'A' * 100000")
    assert res["stdout_truncated"] is True
    assert len(res["stdout"]) <= sysmod.MAX_OUTPUT_CHARS


@pytest.mark.skipif(not _ps_available(), reason="powershell.exe not available")
def test_powershell_truncates_stderr() -> None:
    res = call("powershell", cmd='[Console]::Error.WriteLine("B" * 40000)')
    assert res["stderr_truncated"] is True
    assert len(res["stderr"]) <= sysmod.MAX_OUTPUT_CHARS
    assert res["stderr"]  # it captured something before cutting


@pytest.mark.skipif(not _ps_available(), reason="powershell.exe not available")
def test_powershell_never_returns_the_command() -> None:
    # A command line can carry a token; the result must not echo it back.
    res = call("powershell", cmd="super-secret-value")
    assert "cmd" not in res
    assert "super-secret-value" not in str(res)
    assert res["cmd_len"] == len("super-secret-value")


def test_powershell_rejects_kwargs_and_model_both_ways() -> None:
    # The server may call handler(model) or handler(**model.model_dump()).
    tool = next(t for t in sysmod.TOOLS if t["name"] == "fs_list")
    model = tool["schema"](path=".")
    assert tool["handler"](model) == tool["handler"](path=".")


# ------------------------------------------------------------------ filesystem


def test_fs_roundtrip_in_tmp(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    w = call("fs_write", path="sub/notes.txt", content="line1\nline2", confirm=True)
    assert w["ok"] is True
    assert w["bytes_written"] == len("line1\nline2")

    r = call("fs_read", path="sub/notes.txt")
    assert r["content"] == "line1\nline2"  # newline="" keeps the roundtrip exact
    assert r["truncated"] is False

    lst = call("fs_list", path="sub")
    assert [e["name"] for e in lst["entries"]] == ["notes.txt"]


def test_fs_write_requires_confirm(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = call("fs_write", path="nope.txt", content="x")
    assert res["ok"] is False
    assert res["error"] == "CONFIRM_REQUIRED"
    assert not (tmp_path / "nope.txt").exists()


def test_fs_read_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = call("fs_read", path="does-not-exist.txt")
    assert res == {
        "ok": False,
        "error": "NOT_FOUND",
        "path": str(tmp_path / "does-not-exist.txt"),
    }


def test_fs_list_missing_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    res = call("fs_list", path="ghost")
    assert res == {"ok": False, "error": "NOT_FOUND", "path": str(tmp_path / "ghost")}


def test_fs_read_directory_and_list_file_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    read_dir = call("fs_read", path=".")
    assert read_dir["ok"] is False
    assert read_dir["error"] == "IS_DIRECTORY"
    list_file = call("fs_list", path="f.txt")
    assert list_file["ok"] is False
    assert list_file["error"] == "NOT_A_DIRECTORY"


def test_fs_read_truncates(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_text("B" * 5000, encoding="utf-8")
    res = call("fs_read", path="big.txt", max_bytes=100)
    assert res["truncated"] is True
    assert len(res["content"]) == 100
    assert res["size_bytes"] == 5000


def test_fs_paths_confined_to_cwd(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    # a real file one level above the confinement root
    outside = tmp_path / "escape.txt"
    outside.write_text("nope", encoding="utf-8")
    monkeypatch.chdir(root)

    for name, kwargs in (
        ("fs_list", {"path": str(tmp_path)}),
        ("fs_read", {"path": "../escape.txt"}),
        ("fs_write", {"path": str(outside), "content": "x", "confirm": True}),
    ):
        res = call(name, **kwargs)
        assert res["ok"] is False, name
        assert res["error"] == "PATH_NOT_CONFINED", name
    assert outside.read_text(encoding="utf-8") == "nope"


def test_fs_unconfined_env_allows_any_path(tmp_path, monkeypatch) -> None:
    cwd_dir = tmp_path / "cwd"
    other_dir = tmp_path / "elsewhere"
    cwd_dir.mkdir()
    other_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    monkeypatch.setenv("WINMCP_UNCONFINED", "1")
    target = other_dir / "free.txt"
    assert call("fs_write", path=str(target), content="free", confirm=True)["ok"] is True
    assert call("fs_read", path=str(target))["content"] == "free"


# -------------------------------------------------------------------- registry


@needs_win
def test_registry_get_safe_key() -> None:
    res = call(
        "registry_get",
        path=r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion",
        name="ProgramFilesDir",
    )
    assert res["ok"] is True
    assert res["exists"] is True
    assert res["type"].startswith("REG_")
    assert isinstance(res["value"], str) and res["value"]


@needs_win
def test_registry_get_missing_value() -> None:
    res = call(
        "registry_get",
        path=r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion",
        name="__no_such_value__",
    )
    assert res["ok"] is False
    assert res["error"] == "NOT_FOUND"


def test_registry_get_unknown_hive() -> None:
    res = call("registry_get", path="NOPE\\Foo")
    assert res["ok"] is False
    assert res["error"] == "BAD_REGISTRY_PATH"


def test_registry_get_has_no_write_tool() -> None:
    names = [t["name"] for t in sysmod.TOOLS]
    assert not any("registry" in n and n != "registry_get" for n in names)


# ---------------------------------------------------------------------- notify


def test_notify_headless_is_noop_success(monkeypatch) -> None:
    monkeypatch.setenv("WINMCP_HEADLESS", "1")
    attempted: list[int] = []
    monkeypatch.setattr(
        sysmod, "_send_toast", lambda t, m: attempted.append(1) or False
    )
    assert call("notify", title="t", msg="m") == {"ok": True, "delivered": False}
    assert attempted == []  # headless short-circuits before touching a sender


def test_notify_uses_injected_sender(monkeypatch) -> None:
    monkeypatch.delenv("WINMCP_HEADLESS", raising=False)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sysmod, "_send_toast", lambda t, m: seen.append((t, m)) or True
    )
    assert call("notify", title="Hello", msg="World") == {"ok": True, "delivered": True}
    assert seen == [("Hello", "World")]


def test_notify_sender_failure_still_succeeds(monkeypatch) -> None:
    monkeypatch.delenv("WINMCP_HEADLESS", raising=False)

    def boom(title: str, msg: str) -> bool:
        raise RuntimeError("no session")

    monkeypatch.setattr(sysmod, "_send_toast", boom)
    assert call("notify", title="t", msg="m") == {"ok": True, "delivered": False}


# ------------------------------------------------------------------ scrape_dom


def test_scrape_dom_without_backend_is_stub() -> None:
    sysmod.set_dom_backend(None)
    res = call("scrape_dom", url_hint="https://example.com")
    assert res["ok"] is False
    assert res["error"] == "NO_DOM_BACKEND"


def test_scrape_dom_with_fake_backend() -> None:
    captured: list[str | None] = []

    def fake(hint: str | None) -> dict[str, Any]:
        captured.append(hint)
        return {"ok": True, "url": hint, "title": "Fake Page", "buttons": ["Save"]}

    sysmod.set_dom_backend(fake)
    try:
        res = call("scrape_dom", url_hint="https://a.test")
        assert res == {
            "ok": True,
            "url": "https://a.test",
            "title": "Fake Page",
            "buttons": ["Save"],
        }
        assert captured == ["https://a.test"]
        # the short alias "hint" feeds the same field
        assert sysmod.ScrapeDomArgs(hint="https://b.test").url_hint == "https://b.test"
    finally:
        sysmod.set_dom_backend(None)
    assert sysmod.get_dom_backend() is None


def test_scrape_dom_backend_raising_gives_typed_error() -> None:
    def boom(hint: str | None) -> dict[str, Any]:
        raise RuntimeError("chrome died")

    sysmod.set_dom_backend(boom)
    try:
        res = call("scrape_dom")
        assert res["ok"] is False
        assert res["error"] == "DOM_BACKEND_ERROR"
        assert res["error_type"] == "RuntimeError"
    finally:
        sysmod.set_dom_backend(None)
