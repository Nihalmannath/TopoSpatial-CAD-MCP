"""Regression tests for packaged-client COM connection fallback."""

from unittest.mock import MagicMock, patch

from adapters import AutoCADAdapter


def test_active_object_falls_back_to_configured_clsid():
    adapter = AutoCADAdapter("autocad")
    adapter.config.com_clsid = "{8B4929F8-076F-4AEC-AFEE-8928747B7AE3}"
    unknown = MagicMock()
    dispatch = MagicMock()
    application = MagicMock()
    unknown.QueryInterface.return_value = dispatch

    with (
        patch(
            "adapters.mixins.connection_mixin.win32com.client.GetActiveObject",
            side_effect=OSError("ProgID unavailable"),
        ),
        patch(
            "adapters.mixins.connection_mixin.pythoncom.GetActiveObject",
            return_value=unknown,
        ) as get_by_clsid,
        patch(
            "adapters.mixins.connection_mixin.win32com.client.Dispatch",
            return_value=application,
        ) as wrap_dispatch,
    ):
        assert adapter._get_active_com_object() is application

    get_by_clsid.assert_called_once()
    unknown.QueryInterface.assert_called_once()
    wrap_dispatch.assert_called_once_with(dispatch)


def test_active_object_preserves_normal_progid_path():
    adapter = AutoCADAdapter("autocad")
    application = MagicMock()

    with patch(
        "adapters.mixins.connection_mixin.win32com.client.GetActiveObject",
        return_value=application,
    ) as get_active:
        assert adapter._get_active_com_object() is application

    get_active.assert_called_once_with("AutoCAD.Application")
