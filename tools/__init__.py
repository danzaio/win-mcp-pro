"""win-mcp-pro tools package (orchestrator scaffold; owners extend, do not delete).

Contract: every tool module exposes TOOLS, a list of ToolDef objects with
attributes .name (str), .description (str), .schema (a pydantic BaseModel
subclass), .handler (callable). The registry normalizes dict-style defs.

Dict-style defs (keys name/description/schema/handler, schema already a JSON
schema dict) are accepted too, so a tool module may be written either way::

    TOOLS = [
        {"name": "find", "description": "...", "schema": {...}, "handler": h},
        ToolDef(name="invoke", description="...", schema=InvokeArgs, handler=h),
    ]

Each module registers itself by calling ``tools.register(TOOLS)`` at the bottom
(or the server calls ``tools.load_all_modules()`` once at startup).

``TOOL_REGISTRY`` is the single source of truth the MCP server reads: a flat,
ordered list of normalized dicts. Order is registration order, never sorted.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from typing import Any, Callable, TypedDict

__all__ = [
    "TOOL_REGISTRY",
    "ToolDef",
    "ToolDefError",
    "FIELDS",
    "normalize",
    "register",
    "register_module",
    "json_schema",
    "get",
    "names",
    "clear",
    "load_all_modules",
]

#: Required fields of a tool definition, in the order the server needs them.
FIELDS = ("name", "description", "schema", "handler")

class ToolDef(TypedDict):
    """A tool definition in dict form.

    ``schema`` is either a pydantic ``BaseModel`` subclass (preferred, exported
    with ``model_json_schema()``) or an already built JSON-schema dict.
    """

    name: str
    description: str
    schema: Any
    handler: Callable[..., Any]


#: Normalized tool definitions, populated by :func:`register` / :func:`load_all_modules`.
TOOL_REGISTRY: list[ToolDef] = []


class ToolDefError(ValueError):
    """A tool definition broke the registry contract, or a name was reused."""


# --- reading a definition (attribute style or dict style) ---------------------


def _field(defn: Any, name: str) -> Any:
    """Read ``name`` from an attribute-style or dict-style definition."""
    if isinstance(defn, dict):
        return defn.get(name)
    return getattr(defn, name, None)


def _has_field(defn: Any, name: str) -> bool:
    if isinstance(defn, dict):
        return name in defn
    return hasattr(defn, name)


def _origin(defn: Any) -> str:
    if isinstance(defn, dict):
        return "dict"
    return type(defn).__name__


def _missing(defn: Any) -> list[str]:
    return [f for f in FIELDS if not _has_field(defn, f)]


def normalize(defn: Any) -> dict:
    """Validate one definition and return it as a plain dict.

    ``schema`` is kept as given: a pydantic ``BaseModel`` subclass or an already
    built JSON-schema dict. Use :func:`json_schema` when the JSON form is needed.
    """
    if defn is None:
        raise ToolDefError("tool definition is None")
    missing = _missing(defn)
    if missing:
        raise ToolDefError(f"tool definition ({_origin(defn)}) is missing: {', '.join(missing)}")

    name = _field(defn, "name")
    description = _field(defn, "description")
    schema = _field(defn, "schema")
    handler = _field(defn, "handler")

    if not isinstance(name, str) or not name.strip():
        raise ToolDefError(f"tool definition ({_origin(defn)}) has an empty name")
    if not isinstance(description, str) or not description.strip():
        raise ToolDefError(f"tool {name!r}: description must be a non-empty string")
    if not _is_schema(schema):
        raise ToolDefError(
            f"tool {name!r}: schema must be a pydantic BaseModel subclass or a "
            f"JSON-schema dict, got {_origin(schema)}"
        )
    if not callable(handler):
        raise ToolDefError(f"tool {name!r}: handler must be callable, got {_origin(handler)}")

    return {"name": name, "description": description, "schema": schema, "handler": handler}


def _is_schema(schema: Any) -> bool:
    if isinstance(schema, dict):
        return True
    return isinstance(schema, type) and hasattr(schema, "model_json_schema")


def json_schema(defn: Any) -> dict:
    """Return the JSON-schema dict of a definition (pydantic models are exported)."""
    schema = _field(defn, "schema")
    if isinstance(schema, type) and hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    if isinstance(schema, dict):
        return schema
    raise ToolDefError(f"tool {_field(defn, 'name')!r}: schema is neither a model nor a dict")


# --- registration -------------------------------------------------------------


def _collect(source: Any) -> list:
    """Flatten a source (module, iterable, or single definition) into definitions."""
    if source is None:
        return []
    if hasattr(source, "TOOLS"):
        return _collect(getattr(source, "TOOLS"))
    if isinstance(source, (str, bytes, dict)) or not isinstance(source, Iterable):
        return [source]
    flat: list = []
    for item in source:
        flat.extend(_collect(item))
    return flat


def register(*sources: Any) -> list[dict]:
    """Register tool definitions and return them in the order given.

    A source is a module exposing ``TOOLS``, an iterable of definitions, or a
    single definition. Everything is validated before the registry changes, so a
    bad definition leaves ``TOOL_REGISTRY`` untouched. Duplicate names raise
    :class:`ToolDefError` instead of shadowing an earlier tool.
    """
    pending = _collect(sources)
    normalized = [normalize(defn) for defn in pending]

    counts: dict[str, int] = {}
    for defn in normalized:
        counts[defn["name"]] = counts.get(defn["name"], 0) + 1
    taken = names()
    clashes = sorted(n for n, c in counts.items() if c > 1 or n in taken)
    if clashes:
        raise ToolDefError(
            f"tool name(s) already in use: {', '.join(clashes)}; "
            "call tools.clear() before re-registering"
        )

    TOOL_REGISTRY.extend(normalized)
    return normalized


def register_module(module: Any) -> list[dict]:
    """Shorthand for ``register(module)`` when the module exposes ``TOOLS``."""
    return register(module)


def get(name: str) -> dict | None:
    """Return the registered definition for ``name``, or ``None``."""
    for defn in TOOL_REGISTRY:
        if defn["name"] == name:
            return defn
    return None


def names() -> list[str]:
    """Registered tool names, in registration order."""
    return [defn["name"] for defn in TOOL_REGISTRY]


def clear() -> None:
    """Drop every registration (tests do this between cases)."""
    TOOL_REGISTRY.clear()


def load_all_modules() -> list[str]:
    """Import every module of this package and register its ``TOOLS``.

    Modules are imported in alphabetical order so the registry is deterministic.
    A module without ``TOOLS`` contributes nothing. Import failures propagate:
    a broken tool module must stop the server at startup, not at call time.
    """
    loaded: list[str] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if not hasattr(module, "TOOLS"):
            continue
        register(module)
        loaded.append(info.name)
    return loaded


