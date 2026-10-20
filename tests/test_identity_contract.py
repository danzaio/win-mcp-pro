"""Identity contract regressions — the Phase A slice.

Reproductions from the 12/10 design review, each failing on the pre-fix code
for its stated reason:

* E1  ``find`` minted ids that collided for same-name siblings and for
  same-title windows, so an old reference silently resolved to the wrong
  control.
* E2  ``wait_*`` resolved the window root instead of the element named by the
  id, so a wait could observe the wrong control.
* E3  ``wait_focus`` accepted focus on *any* control.
* E4  ids minted by ``find`` failed the waits id schema, and waits never
  consulted the shared registries.

Everything here runs against fakes; the last test drives the public MCP route
(``find -> invoke -> wait_*``) over the in-memory session, which is the
contract users actually consume.
"""

import asyncio
import json
import types

from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from win_mcp_pro.server import main, registry
from win_mcp_pro.tools import patterns, query, waits

DIGEST = "0123456789ab"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakePattern:
    def __init__(self, owner, name):
        self._owner = owner
        self._name = name

    def Invoke(self):
        self._owner.invoke_count += 1


class FakeElement:
    """Stand-in for uiautomation.Control with a real-ish RuntimeId."""

    def __init__(self, name="", control_type="", runtime_id=None,
                 automation_id="", handle=0, enabled=True, children=None):
        self.Name = name
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.NativeWindowHandle = handle
        self.IsEnabled = enabled
        self.RuntimeId = tuple(runtime_id) if runtime_id is not None else None
        self._children = list(children or [])
        self._patterns = {"invoke"}
        self.invoke_count = 0

    def GetChildren(self):
        return list(self._children)

    def GetRuntimeId(self):
        if self.RuntimeId is None:
            raise RuntimeError("no runtime id for this element")
        return list(self.RuntimeId)

    def GetEnabled(self):
        return self.IsEnabled

    def GetName(self):
        return self.Name

    def GetInvokePattern(self):
        return FakePattern(self, "invoke")

    def GetFocus(self):
        # Legacy shim: the pre-fix focus check accepted any truthy value
        # here. The fixed check compares RuntimeIds and never calls this.
        return True


class FakeUia:
    def __init__(self, root):
        self._root = root

    def GetRootControl(self):
        return self._root

    def GetForegroundControl(self):
        return self._root

    def ControlFromHandle(self, hwnd):
        for child in self._root._children:
            if child.NativeWindowHandle == hwnd:
                return child
        return None

    def GetFocusControl(self):
        return None


class FakeWaitsProvider:
    """Provider seam the waits module talks to, with a legacy get_root shim.

    The shim exists so the pre-fix code path can still run while these tests
    are red: before the fix, ``wait_enabled``/``wait_text`` resolved
    ``get_root(hwnd)``; the fix resolves the element the id names.
    """

    def __init__(self, elements=None, focus=None, root=None):
        self.elements = dict(elements or {})
        self.focus = focus
        self.root = root
        self.focus_calls = 0

    def resolve_element(self, element_id):
        if element_id in self.elements:
            return self.elements[element_id]
        return None

    def get_root(self, hwnd):  # pragma: no cover - red-run shim only
        return self.root

    def get_focus(self):
        self.focus_calls += 1
        return self.focus

    def element_text(self, element):
        return element.GetName()

    def element_enabled(self, element):
        return element.GetEnabled()

    def element_exists(self, element):
        return True


def build_siblings():
    """Pane with two same-name 'Save' buttons and one disabled field."""
    save_a = FakeElement("Save", "ButtonControl", runtime_id=(42, 1),
                         automation_id="save-a")
    save_b = FakeElement("Save", "ButtonControl", runtime_id=(42, 2),
                         automation_id="save-b")
    field = FakeElement("Field", "EditControl", runtime_id=(42, 3),
                        automation_id="field", enabled=False)
    pane = FakeElement("Pane", "PaneControl", runtime_id=(42,), children=[save_a, save_b, field])
    window = FakeElement("Untitled - Notepad", "WindowControl", runtime_id=(1,),
                         handle=1234, children=[pane])
    root = FakeElement("Desktop", "PaneControl", children=[window])
    return types.SimpleNamespace(root=root, window=window, pane=pane,
                                 save_a=save_a, save_b=save_b, field=field)


def install_siblings(monkeypatch):
    parts = build_siblings()
    fake = FakeUia(parts.root)
    monkeypatch.setattr(query, "_uia", fake)
    query._registry_clear()
    before = set(patterns.registered_ids())

    def cleanup():
        query._registry_clear()
        for rid in patterns.registered_ids():
            if rid not in before:
                patterns.unregister_element(rid)

    return parts, cleanup


# ---------------------------------------------------------------------------
# E1: sibling and cross-window identity
# ---------------------------------------------------------------------------


def test_same_name_siblings_get_distinct_runtime_ids(monkeypatch):
    parts, cleanup = install_siblings(monkeypatch)
    try:
        first = query.find(automation_id="save-a")
        second = query.find(automation_id="save-b")
        assert first["ok"] and second["ok"], (first, second)

        id_a = first["element"]["id"]
        id_b = second["element"]["id"]
        # Pre-fix: the digest over (title, path) collided, so both finds
        # returned the same id and the first reference pointed at the second
        # control.
        assert id_a != id_b
        # The id names the control's real identity: window handle + runtime id.
        assert id_a == "1234:42,1"
        assert id_b == "1234:42,2"

        prop = query.get_prop(id_a, "automation_id")
        assert prop["ok"] is True and prop["value"] == "save-a"
    finally:
        cleanup()


def test_same_title_windows_never_cross(monkeypatch):
    field_a = FakeElement("Field", "EditControl", runtime_id=(9, 1))
    field_b = FakeElement("Field", "EditControl", runtime_id=(9, 2),
                          automation_id="field-b")
    window_a = FakeElement("Untitled - Notepad", "WindowControl", runtime_id=(1,),
                           handle=1111, children=[field_a])
    window_b = FakeElement("Untitled - Notepad", "WindowControl", runtime_id=(2,),
                           handle=2222, children=[field_b])
    root = FakeElement("Desktop", "PaneControl", children=[window_a, window_b])
    monkeypatch.setattr(query, "_uia", FakeUia(root))
    query._registry_clear()
    before = set(patterns.registered_ids())
    try:
        first = query.find(name="Field")
        second = query.find(automation_id="field-b")
        assert first["ok"] and second["ok"]
        id_a = first["element"]["id"]
        id_b = second["element"]["id"]
        assert id_a != id_b, "same-title windows produced the same id"
        assert id_a == "1111:9,1" and id_b == "2222:9,2"
        assert query.get_prop(id_a, "handle")["value"] == 1111
        assert query.get_prop(id_b, "handle")["value"] == 2222
    finally:
        query._registry_clear()
        for rid in patterns.registered_ids():
            if rid not in before:
                patterns.unregister_element(rid)


def test_find_id_is_stable_across_repeated_walks(monkeypatch):
    parts, cleanup = install_siblings(monkeypatch)
    try:
        first = query.find(name="Save")
        second = query.find(name="Save")
        assert first["element"]["id"] == second["element"]["id"]
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# E4: one schema for every id producer
# ---------------------------------------------------------------------------

ID_MODULES = (query, waits, patterns)


def _tool_schemas():
    schemas = []
    for module in ID_MODULES:
        for tool in module.TOOLS:
            schema = tool["schema"] if isinstance(tool, dict) else tool.schema
            if hasattr(schema, "model_fields"):
                schemas.append((module.__name__, schema))
    return schemas


def _payload_for(schema, id_value):
    payload = {}
    for name, field in schema.model_fields.items():
        if not field.is_required():
            continue
        if name == "id":
            payload[name] = id_value
            continue
        annotation = field.annotation
        if annotation is str:
            payload[name] = "x"
        elif annotation is int:
            payload[name] = 1
        elif annotation is float:
            payload[name] = 1.0
        else:
            raise AssertionError(f"teach the helper: {schema.__name__}.{name} is {annotation}")
    return payload


def test_every_id_consuming_schema_accepts_what_find_mints():
    """Sweep every tool schema with an ``id`` field for both minted formats."""
    from pydantic import ValidationError

    checked = 0
    failures = []
    for module_name, schema in _tool_schemas():
        if "id" not in schema.model_fields:
            continue
        for fmt in ("1234:42,1", DIGEST):
            checked += 1
            try:
                schema.model_validate(_payload_for(schema, fmt))
            except ValidationError as exc:
                failures.append(f"{module_name}.{schema.__name__} rejected {fmt!r}: {exc}")
    assert checked >= 5, f"the sweep stopped covering schemas: {checked}"
    assert failures == [], failures


def test_digest_ids_validate_in_wait_schemas():
    try:
        waits.WaitTextArgs(s="x", id=DIGEST)
        accepted_text = True
    except Exception:
        accepted_text = False
    assert accepted_text, "wait_text schema rejects a registry id that find can mint"

    try:
        waits.WaitIdArgs(id=DIGEST)
        accepted_id = True
    except Exception:
        accepted_id = False
    assert accepted_id, "wait id schema rejects a registry id that find can mint"


# ---------------------------------------------------------------------------
# E2/E3: waits observe the named element, and focus must match it
# ---------------------------------------------------------------------------


def test_wait_enabled_checks_the_named_child_not_the_window(monkeypatch):
    child = FakeElement("Field", "EditControl", runtime_id=(42, 3), enabled=False)
    decoy_root = FakeElement("Untitled - Notepad", "WindowControl",
                             runtime_id=(1,), enabled=True)
    provider = FakeWaitsProvider(elements={"1234:42,3": child}, root=decoy_root)
    monkeypatch.setattr(waits, "_provider", provider)
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 0)

    # Pre-fix this returned ok=True: the handler observed the enabled window
    # root instead of the disabled child the id names.
    result = waits.handler_wait_enabled(id="1234:42,3", timeout_ms=0)
    assert result["ok"] is False, result


def test_wait_text_reads_the_named_child_not_the_window(monkeypatch):
    child = FakeElement("Cancel", "ButtonControl", runtime_id=(42, 4))
    decoy_root = FakeElement("Save dialog", "WindowControl", runtime_id=(1,))
    provider = FakeWaitsProvider(elements={"1234:42,4": child}, root=decoy_root)
    monkeypatch.setattr(waits, "_provider", provider)
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 0)

    # The window title contains "Save"; the child does not. Pre-fix the wait
    # passed by reading the root.
    result = waits.handler_wait_text(s="Save", id="1234:42,4", timeout_ms=0)
    assert result["ok"] is False, result

    result = waits.handler_wait_text(s="Cancel", id="1234:42,4", timeout_ms=0)
    assert result["ok"] is True, result


def test_wait_focus_requires_the_exact_target(monkeypatch):
    target = FakeElement("Save", "ButtonControl", runtime_id=(42, 1))
    other = FakeElement("Cancel", "ButtonControl", runtime_id=(42, 9))
    provider = FakeWaitsProvider(elements={"1234:42,1": target}, focus=other)
    monkeypatch.setattr(waits, "_provider", provider)
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 0)

    # Pre-fix: any truthy GetFocus() on the focused control passed, so focus
    # on an unrelated control reported success.
    result = waits.handler_wait_focus(id="1234:42,1", timeout_ms=0)
    assert result["ok"] is False, result

    provider.focus = FakeElement("Save", "ButtonControl", runtime_id=(42, 1))
    result = waits.handler_wait_focus(id="1234:42,1", timeout_ms=0)
    assert result["ok"] is True, result

    # Identity that cannot be proven must never report success.
    provider.focus = FakeElement("Save", "ButtonControl", runtime_id=None)
    result = waits.handler_wait_focus(id="1234:42,1", timeout_ms=0)
    assert result["ok"] is False, result


def test_wait_gone_observes_only_the_named_child(monkeypatch):
    child = FakeElement("Toast", "TextControl", runtime_id=(42, 5))
    provider = FakeWaitsProvider(elements={"1234:42,5": child})
    monkeypatch.setattr(waits, "_provider", provider)
    monkeypatch.setattr(waits, "_POLL_INTERVAL_MS", 0)

    # While the child resolves and exists, the wait must keep waiting.
    result = waits.handler_wait_gone(id="1234:42,5", timeout_ms=0)
    assert result["ok"] is False, result

    # The child disappearing under this id is the condition.
    provider.elements.pop("1234:42,5")
    result = waits.handler_wait_gone(id="1234:42,5", timeout_ms=0)
    assert result["ok"] is True, result


# ---------------------------------------------------------------------------
# The public MCP route: find -> invoke -> wait
# ---------------------------------------------------------------------------


def test_mcp_route_find_invoke_wait_unified(monkeypatch):
    parts, cleanup = install_siblings(monkeypatch)
    provider = FakeWaitsProvider(
        elements={
            "1234:42,3": parts.field,
            # decoys: the pre-fix code observed the window root instead of
            # the element the id names
            "__root__": FakeElement("Untitled - Notepad", "WindowControl",
                                    runtime_id=(1,), enabled=True),
        },
        root=FakeElement("Untitled - Notepad", "WindowControl",
                         runtime_id=(1,), enabled=True),
    )
    monkeypatch.setattr(waits, "_provider", provider)
    registry.reset_registry()
    registry.register_module(query.TOOLS)
    registry.register_module(waits.TOOLS)
    registry.register_module(patterns.TOOLS)
    server = main.build_server()

    async def drive():
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            task = asyncio.create_task(
                server.run(*server_streams, server.create_initialization_options(),
                           raise_exceptions=True)
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()

                    async def call(name, args):
                        result = await session.call_tool(name, args)
                        assert result.is_error is False, result.content
                        return json.loads(result.content[0].text)

                    first = await call("find", {"automation_id": "save-a"})
                    second = await call("find", {"automation_id": "save-b"})
                    assert first["ok"] and second["ok"]

                    id_a = first["element"]["id"]
                    id_b = second["element"]["id"]
                    assert id_a != id_b

                    # act: invoke must hit the control the id names
                    invoked = await call("invoke", {"id": id_b})
                    assert invoked["ok"] is True, invoked
                    assert parts.save_b.invoke_count == 1
                    assert parts.save_a.invoke_count == 0

                    # wait: resolves the child named by the id, not the window
                    enabled = await call("wait_enabled",
                                         {"id": "1234:42,3", "timeout_ms": 0})
                    assert enabled["ok"] is False, enabled

                    text = await call("wait_text",
                                      {"s": "Untitled", "id": "1234:42,3",
                                       "timeout_ms": 0})
                    assert text["ok"] is False, text

                    focus = await call("wait_focus",
                                       {"id": "1234:42,1", "timeout_ms": 0})
                    assert focus["ok"] is False, focus
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(asyncio.wait_for(drive(), timeout=20))
    finally:
        cleanup()
