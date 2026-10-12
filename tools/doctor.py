"""Doctor (Phase E): one call that reports whether this machine can run the toolset.

Checks the interpreter, the importability of each runtime dependency, whether
a Chromium browser was found for the isolated ``browser_*`` tools, which OCR
languages are installed for ``ocr_read``, and whether the UIA root control is
reachable. Each check reads through a seam so tests can pin the report
without a real environment; the handler itself never raises.
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict

#: Runtime dependencies the tools load lazily.
_MODULES = ("uiautomation", "comtypes", "mss", "PIL", "winsdk", "websocket")


class DoctorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _module_ok(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
        return True, "imported"
    except Exception as exc:  # noqa: BLE001 - import failure is the report
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def _browser_path() -> str | None:
    from tools import browser

    return browser._find_browser()


def _ocr_languages() -> list[str]:
    try:
        from winsdk.windows.media.ocr import OcrEngine

        return [str(language.language_tag)
                for language in OcrEngine.available_recognizer_languages]
    except Exception:  # noqa: BLE001 - missing winsdk is reported by its own check
        return []


def _uia_reachable() -> tuple[bool, str]:
    try:
        import uiautomation

        root = uiautomation.GetRootControl()
        return (root is not None, "root control reachable" if root else "no root control")
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def handler_doctor(args: Any) -> dict:
    DoctorArgs(**dict(args or {}))
    checks: list[dict] = [{
        "name": "python",
        "ok": sys.version_info >= (3, 11),
        "detail": sys.version.split()[0],
    }]
    for module in _MODULES:
        ok, detail = _module_ok(module)
        checks.append({"name": "module:%s" % module, "ok": ok, "detail": detail})
    browser = _browser_path()
    checks.append({"name": "browser", "ok": bool(browser),
                   "detail": browser or "no Edge/Chrome found"})
    languages = _ocr_languages()
    checks.append({"name": "ocr_languages", "ok": bool(languages),
                   "detail": ", ".join(languages) or "none installed"})
    ok, detail = _uia_reachable()
    checks.append({"name": "uia", "ok": ok, "detail": detail})
    passed = sum(1 for check in checks if check["ok"])
    return {"ok": passed == len(checks), "checks": checks,
            "summary": "%d/%d checks ok" % (passed, len(checks))}


def _tool(name: str, description: str, schema, handler) -> dict:
    return {"name": name, "description": description, "schema": schema,
            "handler": handler}


TOOLS = [
    _tool("doctor",
          "Report whether this machine can run the toolset: interpreter, "
          "runtime dependencies, Chromium for the browser tools, installed "
          "OCR languages and UIA reachability - one JSON report.",
          DoctorArgs, handler_doctor),
]
