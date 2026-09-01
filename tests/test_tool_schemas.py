"""MCP schema regressions for native arrays and discriminated design actions."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastmcp import FastMCP

from mcp_tools.tools.design import register_design_tools
from mcp_tools.tools.drawing import register_drawing_tools
from mcp_tools.tools.session import register_session_tools
from server import MCP_INSTRUCTIONS


def _parameters(register: Any, name: str) -> Dict[str, Any]:
    mcp = FastMCP(name="schema-test")
    register(mcp)
    tools = asyncio.run(mcp.list_tools())
    return next(tool.parameters for tool in tools if tool.name == name)


def test_manage_design_schema_is_discriminated_by_action() -> None:
    parameters = _parameters(register_design_tools, "manage_design")
    request = parameters["properties"]["request"]

    assert request["discriminator"]["propertyName"] == "action"
    assert set(request["discriminator"]["mapping"]) == {
        "inspect",
        "get_context",
        "get_result",
        "create",
        "modify",
        "validate",
        "preview",
        "apply",
        "cancel",
        "rollback",
        "metrics",
    }


def test_manage_session_schema_accepts_native_arrays_and_legacy_strings() -> None:
    parameters = _parameters(register_session_tools, "manage_session")
    variants = parameters["properties"]["operations"]["anyOf"]

    assert {variant.get("type") for variant in variants} >= {"string", "array"}


def test_draw_entities_schema_accepts_native_arrays_and_legacy_strings() -> None:
    parameters = _parameters(register_drawing_tools, "draw_entities")
    variants = parameters["properties"]["entities"]["anyOf"]

    assert {variant.get("type") for variant in variants} >= {
        "string",
        "object",
        "array",
    }


def test_server_instructions_expose_in_place_modification_rule() -> None:
    """Every MCP client receives the workflow rule, not only repository agents."""
    assert "never create a new drawing" in MCP_INSTRUCTIONS
    assert "bounded topology neighborhood" in MCP_INSTRUCTIONS
    assert "Stop after preview" in MCP_INSTRUCTIONS
