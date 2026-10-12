"""Packaging constraints for the public install surface."""

import re
import tomllib
from pathlib import Path


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", requirement)
    assert match is not None
    return match.group(0).lower().replace("_", "-")


def test_runtime_dependencies_bound_supported_apis_and_keep_pytest_dev_only():
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    runtime = {_requirement_name(item): item for item in project["dependencies"]}
    dev = project["optional-dependencies"]["dev"]

    assert runtime["mcp"] == "mcp>=2.3,<3"
    assert runtime["pydantic"] == "pydantic>=2,<3"
    assert "pytest" not in runtime
    assert "pytest>=8,<10" in dev
