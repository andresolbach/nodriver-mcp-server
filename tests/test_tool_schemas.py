"""Contract tests for the MCP tool surface.

These run without a browser: they assert on the schemas the server actually
hands to a client, which is the thing an agent reasons over. A tool whose
parameters have no descriptions is not a broken import — it is a tool the model
has to guess at, so it gets caught here rather than in production.
"""

from __future__ import annotations

import json

import pytest

from nodriver_mcp.server import mcp


@pytest.fixture(scope="module")
def tools(anyio_backend=None):
    import asyncio

    return asyncio.run(mcp.list_tools())


@pytest.fixture(scope="module")
def by_name(tools):
    return {t.name: t for t in tools}


def _params(tool) -> dict:
    return (tool.inputSchema or {}).get("properties", {})


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------

def test_tool_count(tools):
    # 62 browser tools + browser_status, which the routing layer consumes and
    # hides from clients.
    assert len(tools) == 63


def test_tool_names_are_unique(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


def test_no_tool_was_renamed_by_accident(by_name):
    """Tool names are public API — a rename silently breaks every user's prompts."""
    expected = {
        "block_resources", "browser_status", "bypass_insecure_warning",
        "cf_verify", "clear_cookies",
        "click", "click_at", "close_browser", "close_page", "create_profile",
        "select_option", "set_checked", "list_frames",
        "delete_profile", "disable_console_collection", "drag", "emulate",
        "emulate_device", "enable_console_collection", "evaluate_script", "fill",
        "fill_form", "get_console_message", "get_cookies", "get_local_storage",
        "get_network_request", "get_page_content", "handle_dialog", "hover",
        "list_console_messages", "list_network_requests", "list_pages",
        "list_profiles", "list_sessions", "load_session", "manage_extensions",
        "navigate_page", "new_page", "performance_start_trace",
        "performance_stop_trace", "press_key", "query_selector", "reset_emulation",
        "resize_page", "save_pdf", "save_session", "scroll_page",
        "scroll_to_selector", "select_page", "set_browser_flags", "set_cookie",
        "set_local_storage", "take_memory_snapshot", "take_screenshot",
        "take_snapshot", "type_text", "upload_file", "use_profile",
        "use_running_browser", "get_computed_styles",
        "use_temp_profile", "wait_for", "wait_for_selector",
    }
    assert set(by_name) == expected


# ---------------------------------------------------------------------------
# Descriptions — what the model reads
# ---------------------------------------------------------------------------

def test_every_tool_has_a_substantial_description(tools):
    thin = [t.name for t in tools if not t.description or len(t.description) < 80]
    assert thin == [], f"tools with a too-thin description: {thin}"


def test_every_parameter_is_described(tools):
    missing = [
        f"{t.name}.{pname}"
        for t in tools
        for pname, spec in _params(t).items()
        if not spec.get("description")
    ]
    assert missing == [], f"parameters with no description: {missing}"


def test_descriptions_are_dedented(tools):
    """The source file's indentation must not ship in every request.

    Checked by re-running cleandoc: if that changes the text, the description
    still carries the leading whitespace of the docstring it came from. An
    intentionally indented block, such as a command example, is left alone,
    because cleandoc preserves relative indentation.
    """
    import inspect as _inspect

    undedented = [
        t.name
        for t in tools
        if t.description and _inspect.cleandoc(t.description) != t.description
    ]
    assert undedented == []


def test_descriptions_do_not_leak_a_raw_args_block(tools):
    """Parameter docs belong in the schema, not in the prose blob."""
    leaked = [t.name for t in tools if t.description and "\nArgs:" in t.description]
    assert leaked == []


# ---------------------------------------------------------------------------
# Behaviour hints — what the client reads
# ---------------------------------------------------------------------------

def test_every_tool_is_annotated(tools):
    missing = [t.name for t in tools if not t.annotations or not t.annotations.title]
    assert missing == []


def test_read_only_tools_are_marked(by_name):
    for name in ("take_snapshot", "list_pages", "get_cookies", "query_selector"):
        assert by_name[name].annotations.readOnlyHint is True, name


def test_destructive_tools_are_marked(by_name):
    for name in ("delete_profile", "clear_cookies", "close_browser"):
        assert by_name[name].annotations.destructiveHint is True, name


def test_read_only_tools_are_never_destructive(tools):
    both = [
        t.name
        for t in tools
        if t.annotations and t.annotations.readOnlyHint and t.annotations.destructiveHint
    ]
    assert both == []


# ---------------------------------------------------------------------------
# Constrained values — what stops a wrong call before it is made
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name,param,expected",
    [
        ("navigate_page", "type", {"url", "back", "forward", "reload"}),
        ("navigate_page", "handle_before_unload", {"accept", "dismiss"}),
        ("handle_dialog", "action", {"accept", "dismiss"}),
        ("scroll_page", "direction", {"up", "down"}),
        ("take_screenshot", "format", {"png", "jpeg", "webp"}),
        ("get_page_content", "format", {"text", "html"}),
        ("manage_extensions", "action", {"list", "on", "off", "load", "unload"}),
        ("emulate", "color_scheme", {"", "dark", "light", "auto"}),
    ],
)
def test_enum_parameters_are_constrained(by_name, tool_name, param, expected):
    spec = _params(by_name[tool_name])[param]
    assert set(spec.get("enum", [])) == expected


def test_list_enum_parameters_are_constrained(by_name):
    """Enums inside arrays live on the item schema."""
    items = _params(by_name["block_resources"])["types"]
    assert "image" in json.dumps(items) and "stylesheet" in json.dumps(items)

    console = json.dumps(_params(by_name["list_console_messages"])["types"])
    assert '"warning"' in console, "CDP emits 'warning', never 'warn'"
    assert '"warn"' not in console.replace('"warning"', "")


def test_network_resource_types_use_real_cdp_values(by_name):
    """Every advertised value must exist in nodriver's ResourceType.

    The stored request type comes from that enum. If the two ever drift apart,
    the resource_types filter silently matches nothing — which is exactly the
    bug this guards against.
    """
    from nodriver.cdp.network import ResourceType

    real = {e.value for e in ResourceType}
    spec = _params(by_name["list_network_requests"])["resource_types"]
    advertised = set()

    def collect(node):
        if isinstance(node, dict):
            advertised.update(node.get("enum", []))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(spec)
    assert advertised, "resource_types advertises no values at all"
    assert advertised <= real, f"not real CDP resource types: {sorted(advertised - real)}"


def test_fill_form_elements_are_typed(by_name):
    """list[dict] would make the model guess the keys; a model publishes them."""
    schema = by_name["fill_form"].inputSchema
    blob = json.dumps(schema)
    assert "$defs" in schema
    assert '"uid"' in blob and '"value"' in blob


def test_numeric_bounds_are_declared(by_name):
    assert _params(by_name["take_screenshot"])["quality"]["maximum"] == 100
    assert _params(by_name["select_page"])["page_id"]["minimum"] == 0
    assert _params(by_name["wait_for"])["text"]["minItems"] == 1


def test_device_presets_do_not_ship_their_own_q_values():
    """Regression: every mobile request carried a malformed Accept-Language.

    Chrome generates the q-values itself from a bare language list, so a preset
    supplying "en-US,en;q=0.9" produced "en-US,en;q=0.9;q=0.9" on the wire and
    ["en-US", "en;q=0.9"] in navigator.languages. On a server whose entire point
    is not standing out, that is a one-header signature.
    """
    from nodriver_mcp.server import _DEVICE_PRESETS

    for name, preset in _DEVICE_PRESETS.items():
        value = preset.get("accept_language", "")
        assert ";" not in value, (
            f"{name} ships q-values Chrome will duplicate: {value!r}"
        )
        assert value, f"{name} has no accept_language"
