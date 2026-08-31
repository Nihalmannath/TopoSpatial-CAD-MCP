"""Tests for connection-safe architecture capability reporting."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_tools.tools import session as session_tools


def test_status_remains_connected_when_optional_capability_probe_fails() -> None:
    adapter = MagicMock()
    adapter.get_architecture_capabilities.side_effect = RuntimeError(
        "AEC registry unavailable"
    )

    with (
        patch.object(session_tools, "get_adapter", return_value=adapter),
        patch(
            "adapters.adapter_manager.get_active_cad_type",
            return_value="autocad",
        ),
    ):
        result = session_tools._status({})

    assert result["success"] is True
    assert result["status"] == {"autocad": "connected"}
    assert result["architecture"] == {
        "native_aec": False,
        "warning": "AEC registry unavailable",
    }


def test_capabilities_returns_native_authoring_details() -> None:
    adapter = MagicMock(cad_type="autocad")
    adapter.get_architecture_capabilities.return_value = {
        "native_aec": True,
        "aec_api_version": "8.6",
        "styles": {"wall": ["Standard"]},
    }

    with patch.object(session_tools, "get_adapter", return_value=adapter):
        result = session_tools._capabilities({"include_styles": True})

    assert result["success"] is True
    assert result["cad_type"] == "autocad"
    assert result["native_architecture"]["aec_api_version"] == "8.6"
