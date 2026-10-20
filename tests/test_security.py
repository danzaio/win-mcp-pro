"""Task 12 tests: security + UIPI.

Self-contained on purpose: the repo scaffold (win_mcp_pro/server/__init__.py,
tests/__init__.py) is owned by Task 1 and may not exist yet, so the
repo root is bootstrapped onto sys.path here. server.security must be
import-safe on any platform.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from win_mcp_pro.server import security  # noqa: E402


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
        allow, block = security.parse_tool_filter(["--exclude-tools", "invoke,click"])
        assert block == {"invoke", "click"}
        assert allow is None

    def test_exclude_tools_equals_form(self):
        allow, block = security.parse_tool_filter(["--exclude-tools=invoke"])
        assert block == {"invoke"}

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

# ---------------------------------------------------------------- --read-only
class TestReadOnlyProfile:
    def test_every_read_only_tool_exists(self):
        """A renamed tool must not silently shrink the safe profile."""
        from win_mcp_pro import tools

        tools.clear()
        tools.load_all_modules()
        assert security.READ_ONLY_TOOLS <= set(tools.names())

    def test_read_only_profile_has_no_side_effects(self):
        forbidden = {"click", "double_click", "move", "scroll", "shortcut", "type_text",
                     "key_hold", "clipboard_set", "set_value", "invoke", "form_fill",
                     "app_launch", "win_close", "choose_file", "paste_text", "replay"}
        assert not (security.READ_ONLY_TOOLS & forbidden)

    def test_flag_becomes_an_allowlist(self, monkeypatch):
        from win_mcp_pro.server import main as server_main
        from win_mcp_pro.server import registry

        calls: list[dict] = []
        monkeypatch.setattr(registry, "run_server", lambda **kwargs: calls.append(kwargs))
        assert server_main.main(["--read-only"]) == 0
        assert set(calls[0]["allowlist"]) == security.READ_ONLY_TOOLS

    def test_flag_refuses_to_mix_with_an_explicit_allowlist(self, monkeypatch):
        from win_mcp_pro.server import main as server_main
        from win_mcp_pro.server import registry

        monkeypatch.setattr(registry, "run_server", lambda **kwargs: None)
        with pytest.raises(SystemExit) as raised:
            server_main.main(["--read-only", "--tools", "click"])
        assert raised.value.code == 2


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
