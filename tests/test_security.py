"""Task 12 tests: security + UIPI.

Self-contained on purpose: the repo scaffold (server/__init__.py,
tests/__init__.py) is owned by Task 1 and may not exist yet, so the
repo root is bootstrapped onto sys.path here. server.security must be
import-safe on any platform.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server import security  # noqa: E402


# ---------------------------------------------------------------- parse_tool_filter
class TestParseToolFilter:
    def test_empty_argv_means_no_restriction(self):
        assert security.parse_tool_filter([]) == (None, None)

    def test_tools_comma_list(self):
        allow, block = security.parse_tool_filter(["--tools", "invoke,snapshot,find"])
        assert allow == {"invoke", "snapshot", "find"}
        assert block is None

    def test_equals_form(self):
        allow, block = security.parse_tool_filter(["--tools=invoke,snapshot"])
        assert allow == {"invoke", "snapshot"}

    def test_exclude_tools_comma_list(self):
        allow, block = security.parse_tool_filter(["--exclude-tools", "kill_proc,powershell"])
        assert block == {"kill_proc", "powershell"}
        assert allow is None

    def test_exclude_tools_equals_form(self):
        allow, block = security.parse_tool_filter(["--exclude-tools=kill_proc"])
        assert block == {"kill_proc"}

    def test_both_flags_together(self):
        allow, block = security.parse_tool_filter(
            ["--tools", "invoke,find", "--exclude-tools", "find"]
        )
        assert allow == {"invoke", "find"}
        assert block == {"find"}

    def test_whitespace_and_empty_entries_dropped(self):
        allow, block = security.parse_tool_filter(
            ["--tools", " invoke , , snapshot ,"]
        )
        assert allow == {"invoke", "snapshot"}

    def test_unknown_args_are_ignored(self):
        allow, block = security.parse_tool_filter(
            ["--transport", "stdio", "--tools", "invoke"]
        )
        assert allow == {"invoke"}

    def test_empty_value_raises(self):
        # an empty allowlist must never mean "allow everything"
        with pytest.raises(ValueError):
            security.parse_tool_filter(["--tools", ""])
        with pytest.raises(ValueError):
            security.parse_tool_filter(["--tools", " , "])
        with pytest.raises(ValueError):
            security.parse_tool_filter(["--exclude-tools", ""])

    def test_missing_value_raises(self):
        with pytest.raises(ValueError):
            security.parse_tool_filter(["--tools"])

    def test_repeated_flag_raises(self):
        with pytest.raises(ValueError):
            security.parse_tool_filter(["--tools", "a", "--tools", "b"])


# ---------------------------------------------------------------- enforce_filter
class TestEnforceFilter:
    def test_no_filters_allows_everything(self):
        assert security.enforce_filter("kill_proc", None, None) is True

    def test_allowlist_membership(self):
        allow = {"invoke", "find"}
        assert security.enforce_filter("find", allow, None) is True
        assert security.enforce_filter("kill_proc", allow, None) is False

    def test_blocklist_rejects(self):
        block = {"kill_proc", "powershell"}
        assert security.enforce_filter("kill_proc", None, block) is False
        assert security.enforce_filter("find", None, block) is True

    def test_blocklist_wins_over_allowlist(self):
        allow = {"invoke", "find"}
        block = {"find"}
        # the critical test: allowlisted AND blocklisted must be rejected
        assert security.enforce_filter("find", allow, block) is False
        assert security.enforce_filter("invoke", allow, block) is True

    def test_empty_allowlist_denies_all_fail_closed(self):
        # only reachable if a caller builds an empty set directly
        assert security.enforce_filter("find", set(), None) is False

    def test_dedupe_vs_copy_is_not_required(self):
        allow = {"a"}
        assert security.enforce_filter("a", allow, set()) is True


# ---------------------------------------------------------------- redact
class TestRedact:
    def test_masks_known_keys(self):
        out = security.redact({"api_key": "sk-123", "tool": "invoke"})
        assert out["api_key"] == security.MASK
        assert out["tool"] == "invoke"
        assert "sk-123" not in repr(out)

    def test_case_insensitive_match(self):
        out = security.redact({"Authorization": "Bearer xyz", "PASSWORD": "hunter2"})
        assert out["Authorization"] == security.MASK
        assert out["PASSWORD"] == security.MASK
        assert "hunter2" not in repr(out)

    def test_nested_structures(self):
        payload = {
            "cfg": {"token": "tkn-unique-1", "nested": {"secret": "s3cr-unique-2",
                                                        "keep": 42}},
            "items": [{"password": "p4ss-unique-3"}, {"note": "plain"}],
        }
        out = security.redact(payload)
        assert out["cfg"]["token"] == security.MASK
        assert out["cfg"]["nested"]["secret"] == security.MASK
        assert out["cfg"]["nested"]["keep"] == 42
        assert out["items"][0]["password"] == security.MASK
        assert out["items"][1]["note"] == "plain"
        dumped = repr(out)
        for secret in ("tkn-unique-1", "s3cr-unique-2", "p4ss-unique-3"):
            assert secret not in dumped

    def test_non_dicts_pass_through(self):
        assert security.redact("plain text") == "plain text"
        assert security.redact(7) == 7
        assert security.redact(None) is None

    def test_original_not_mutated(self):
        src = {"api_key": "sk-1", "sub": {"token": "t"}}
        security.redact(src)
        assert src == {"api_key": "sk-1", "sub": {"token": "t"}}


# ---------------------------------------------------------------- check_uipi (Windows)
WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="ctypes UIPI check is Windows-only")


@WIN_ONLY
class TestCheckUipiSelf:
    def test_self_integrity_level_returns_value(self):
        # check_uipi(os.getpid()) must inspect our own token and report a level
        result = security.check_uipi(os.getpid())
        assert isinstance(result, dict)
        assert result["ok"] is True
        assert "code" not in result
        assert isinstance(result["detail"], str) and result["detail"]

    def test_integrity_label_helper(self):
        label = security.integrity_label(os.getpid())
        assert label in {"untrusted", "low", "medium", "medium-plus", "high",
                         "system", "protected-process", "unknown"}


@WIN_ONLY
class TestCheckUipiElevatedTarget:
    def test_elevation_required_typed_error(self, monkeypatch):
        # target at higher integrity (0x3000/0x4000) than our own (0x2000)
        monkeypatch.setattr(security, "integrity_rid",
                            lambda pid: 0x3000 if pid == 4242 else 0x2000)
        result = security.check_uipi(4242)
        assert result["ok"] is False
        assert result["code"] == "ELEVATION_REQUIRED"
        assert "integrity" in result["detail"].lower()
        assert "admin" in result["detail"].lower()  # actionable instruction

    def test_system_target_is_elevation_required(self, monkeypatch):
        monkeypatch.setattr(security, "integrity_rid",
                            lambda pid: 0x4000 if pid == 7 else 0x2000)
        result = security.check_uipi(7)
        assert result["ok"] is False
        assert result["code"] == "ELEVATION_REQUIRED"

    def test_lower_target_is_allowed(self, monkeypatch):
        monkeypatch.setattr(security, "integrity_rid",
                            lambda pid: 0x1000 if pid == 4242 else 0x2000)
        result = security.check_uipi(4242)
        assert result["ok"] is True

    def test_equal_target_is_allowed(self, monkeypatch):
        monkeypatch.setattr(security, "integrity_rid",
                            lambda pid: 0x3000)
        assert security.check_uipi(4242)["ok"] is True

    def test_failed_read_is_fail_closed(self, monkeypatch):
        def boom(pid):
            raise OSError("access denied")
        monkeypatch.setattr(security, "integrity_rid", boom)
        result = security.check_uipi(4242)
        assert result["ok"] is False
        assert result["code"] == "INTEGRITY_CHECK_FAILED"

    def test_invalid_pid(self):
        assert security.check_uipi(0)["ok"] is False
        assert security.check_uipi(0)["code"] == "INVALID_PID"
        assert security.check_uipi(-3)["code"] == "INVALID_PID"
        assert security.check_uipi(True)["code"] == "INVALID_PID"  # bool is not a pid

    def test_never_reads_out_of_range_rid_silently(self, monkeypatch):
        # unknown RID must still produce a comparable answer, not a crash
        monkeypatch.setattr(security, "integrity_rid",
                            lambda pid: 0x9999 if pid == 4242 else 0x2000)
        result = security.check_uipi(4242)
        assert result["ok"] is False
        assert result["code"] == "ELEVATION_REQUIRED"
