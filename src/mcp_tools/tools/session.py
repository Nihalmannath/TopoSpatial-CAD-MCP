"""
Unified session management tool.

Replaces 7 individual tools with 1:
- manage_session: connect_cad, disconnect_cad, list_supported_cads,
                  get_connection_status, zoom_extents, undo, redo (7→1)

Covers all non-content operations: connection lifecycle, view, and history.
"""

import json
import logging
import webbrowser
from typing import Annotated, Optional, Dict, Any, Callable, List, Tuple, Union, Literal

from pydantic import BaseModel, ConfigDict, Field

from core import get_supported_cads, CADConnectionError, CADBusyError, get_config
from adapters.adapter_manager import (
    get_cad_instances,
    get_adapter,
    shutdown_all,
    auto_detect_cad,
)
from adapters.com_worker import run_com

logger = logging.getLogger(__name__)


class _SessionOperationBase(BaseModel):
    """Strict base for native structured session operations."""

    model_config = ConfigDict(extra="forbid")


class SimpleSessionOperation(_SessionOperationBase):
    action: Literal[
        "connect",
        "start",
        "disconnect",
        "status",
        "list_supported",
        "check_running",
        "zoom_extents",
        "screenshot",
        "export_view",
    ]


class CapabilitiesSessionOperation(_SessionOperationBase):
    action: Literal["capabilities"]
    include_styles: bool = True


class HistorySessionOperation(_SessionOperationBase):
    action: Literal["undo", "redo"]
    count: int = Field(default=1, ge=1, le=100)


class DashboardSessionOperation(_SessionOperationBase):
    action: Literal["open_dashboard"]
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8888, ge=1, le=65535)


SessionOperation = Annotated[
    Union[
        SimpleSessionOperation,
        CapabilitiesSessionOperation,
        HistorySessionOperation,
        DashboardSessionOperation,
    ],
    Field(discriminator="action"),
]


def _refresh_cache_safe():
    """Refresh dashboard cache, ignoring errors if dashboard not loaded."""
    try:
        from web.api import refresh_dashboard_cache

        refresh_dashboard_cache()
    except Exception as e:
        logger.debug(f"Dashboard cache refresh skipped: {e}")


# ========== Action Handlers ==========


def _connect(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Attach to an already-running CAD application.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    try:
        def _do():
            _adapter = get_adapter(only_if_running=True, allow_launch=False)
            from adapters.adapter_manager import get_active_cad_type

            cad_type = get_active_cad_type()
            logger.info(f"Connected to {cad_type}")
            _refresh_cache_safe()
            return {"success": True, "detail": f"Connected to {cad_type}"}

        return run_com(_do, cad_type="autocad")
    except CADBusyError as e:
        return {"success": False, "detail": f"CAD_BUSY: {e.reason}"}
    except Exception as e:
        return {
            "success": False,
            "detail": f"{e}. Use session 'start' to explicitly launch a new AutoCAD process.",
        }


def _start(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Explicitly launch and connect to a CAD application.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    try:
        def _do():
            _adapter = get_adapter(only_if_running=False, allow_launch=True)
            from adapters.adapter_manager import get_active_cad_type

            cad_type = get_active_cad_type()
            logger.info(f"Started and connected to {cad_type}")
            _refresh_cache_safe()
            return {"success": True, "detail": f"Started and connected to {cad_type}"}

        return run_com(_do, cad_type="autocad")
    except CADBusyError as e:
        return {"success": False, "detail": f"CAD_BUSY: {e.reason}"}
    except Exception as e:
        return {"success": False, "detail": str(e)}


def _disconnect(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Disconnect from the active CAD application.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    try:
        shutdown_all()
        _refresh_cache_safe()
        return {"success": True, "detail": "Disconnected."}
    except Exception as e:
        logger.error(f"Disconnection error: {e}")
        return {"success": False, "detail": f"Error disconnecting: {e}"}


def _status(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return the current connection status for all CAD instances.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), status (dict mapping cad type to status string).
    """
    # Adapter state is deliberately thread-local because COM proxies cannot be
    # shared across FastMCP worker apartments.  Resolve the running application
    # on this worker instead of reporting another worker's cache as disconnected.
    try:
        adapter = get_adapter(only_if_running=True)
        from adapters.adapter_manager import get_active_cad_type

        cad_type = get_active_cad_type()
        result: Dict[str, Any] = {
            "success": True,
            "status": {cad_type: "connected"},
        }
        capability_method = getattr(adapter, "get_architecture_capabilities", None)
        if callable(capability_method):
            try:
                result["architecture"] = capability_method(include_styles=False)
            except Exception as exc:
                # Capability probing is diagnostic; it must never turn a valid
                # base CAD connection into a false "disconnected" response.
                result["architecture"] = {
                    "native_aec": False,
                    "warning": str(exc),
                }
        return result
    except Exception:
        return {"success": True, "status": {"all": "disconnected"}}


def _capabilities(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return product and native-authoring capabilities for the active CAD."""
    try:
        adapter = get_adapter(only_if_running=True)
        architecture = getattr(adapter, "get_architecture_capabilities", None)
        result = {
            "success": True,
            "cad_type": getattr(adapter, "cad_type", "unknown"),
            "standard_entities": True,
            "native_architecture": (
                architecture(include_styles=bool(spec.get("include_styles", True)))
                if callable(architecture)
                else {"native_aec": False, "supported_objects": []}
            ),
        }
        return result
    except Exception as exc:
        return {"success": False, "detail": str(exc)}


def _list_supported(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return the list of supported CAD application types.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), supported (list).
    """
    supported = get_supported_cads()
    return {"success": True, "supported": list(supported)}


def _zoom_extents(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Zoom the view to show all entities in the drawing.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    def _do():
        adapter = get_adapter(only_if_running=True)
        success = adapter.zoom_extents()
        return {
            "success": success,
            "detail": "Zoomed to extents" if success else "Failed to zoom",
        }
    return run_com(_do, cad_type="autocad")


def _undo(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Undo one or more recent actions.

    Args:
        spec: Operation spec with keys: count (int, optional, default 1).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    def _do():
        adapter = get_adapter(only_if_running=True)
        count = spec.get("count", 1)
        success = adapter.undo(count=count)
        if success:
            detail = "Action undone" if count == 1 else f"{count} actions undone"
        else:
            detail = "Failed to undo"
        return {"success": success, "detail": detail}
    return run_com(_do, cad_type="autocad")


def _redo(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Redo one or more previously undone actions.

    Args:
        spec: Operation spec with keys: count (int, optional, default 1).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    def _do():
        adapter = get_adapter(only_if_running=True)
        count = spec.get("count", 1)
        success = adapter.redo(count=count)
        if success:
            detail = "Action redone" if count == 1 else f"{count} actions redone"
        else:
            detail = "Failed to redo"
        return {"success": success, "detail": detail}
    return run_com(_do, cad_type="autocad")


def _screenshot(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Capture window screenshot including UI chrome."""
    def _do():
        adapter = get_adapter(only_if_running=True)
        result = adapter.get_screenshot()
        return {
            "success": True,
            "detail": f"Screenshot saved to {result['path']}",
            "path": result["path"],
            "data": result["data"],
        }
    try:
        return run_com(_do, cad_type="autocad")
    except Exception as e:
        return {"success": False, "detail": str(e)}


def _export_view(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Export view using internal rendering (no UI, works when obscured)."""
    def _do():
        adapter = get_adapter(only_if_running=True)
        result = adapter.export_view()
        return {
            "success": True,
            "detail": f"View exported to {result['path']}",
            "path": result["path"],
            "data": result["data"],
        }
    try:
        return run_com(_do, cad_type="autocad")
    except Exception as e:
        return {"success": False, "detail": str(e)}


def _check_running(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Check if any supported CAD is running without launching it."""
    auto_detect_cad(only_if_running=True)
    instances = get_cad_instances()

    if instances:
        _refresh_cache_safe()
        return {
            "success": True,
            "any_running": True,
            "running_cad_types": list(instances.keys()),
        }

    return {"success": True, "any_running": False, "running_cad_types": []}


def _open_dashboard(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Open the web dashboard in the default browser."""
    config = get_config()
    host = spec.get("host", config.dashboard.host)
    port = spec.get("port", config.dashboard.port)
    url = f"http://{host}:{port}"
    try:
        webbrowser.open(url)
        return {"success": True, "detail": f"Dashboard opened at {url}"}
    except Exception as e:
        return {"success": False, "detail": f"Failed to open dashboard: {e}"}


# Dispatch table: action -> (handler, required_fields)
SESSION_DISPATCH: Dict[str, Tuple[Callable, List[str]]] = {
    "connect": (_connect, []),
    "start": (_start, []),
    "disconnect": (_disconnect, []),
    "status": (_status, []),
    "list_supported": (_list_supported, []),
    "capabilities": (_capabilities, []),
    "check_running": (_check_running, []),
    "zoom_extents": (_zoom_extents, []),
    "undo": (_undo, []),
    "redo": (_redo, []),
    "screenshot": (_screenshot, []),
    "export_view": (_export_view, []),
    "open_dashboard": (_open_dashboard, []),
}


def _validate_required_fields(
    spec: Dict[str, Any], required: List[str], action: str
) -> Optional[str]:
    """Validate that required fields are present in spec.

    Args:
        spec: Operation spec dict to validate.
        required: List of field names that must be present.
        action: Action name used in the error message.

    Returns:
        Error message string if any fields are missing, otherwise None.
    """
    missing = [f for f in required if f not in spec]
    if missing:
        return f"'{action}' requires fields: {', '.join(missing)}"
    return None


# ========== Tool Registration ==========


def register_session_tools(mcp):
    """Register unified session management tool with FastMCP."""

    @mcp.tool()
    def manage_session(
        operations: Union[str, SessionOperation, List[SessionOperation]],
    ) -> str:
        """
        Manage CAD session: connection, view, and history operations.

        Args:
            operations: JSON array of operations. Each object must include
                        an "action" field.

                Supported actions and their fields:

                Connection:
                - connect:        (no fields) — attaches to already-running CAD (only_if_running=True)
                - start:          (no fields) — explicitly launches and connects to CAD
                - disconnect:     (no fields) — disconnects from CAD
                - status:         (no fields) — shows connection status
                - list_supported: (no fields) — lists available CAD applications
                - capabilities:   [include_styles] — reports native ACA support and styles
                - check_running:  (no fields) — checks for running CAD without launching

                View:
                - zoom_extents:   (no fields) — zoom to show all entities
                - screenshot:     (no fields) — capture window screenshot (includes UI chrome)
                - export_view:    (no fields) — export view using internal rendering (pure drawing, works when window is obscured)

                History:
                - undo:           [count] (default count: 1)
                - redo:           [count] (default count: 1)

                Dashboard:
                - open_dashboard: [host, port] — open web dashboard in browser (default from config.json)

                Example:
                [
                    {"action": "connect"},
                    {"action": "zoom_extents"},
                    {"action": "undo", "count": 3}
                ]

                Operations execute sequentially.

        Returns:
            JSON result with per-operation status
        """
        try:
            ops_data = (
                json.loads(operations) if isinstance(operations, str) else operations
            )
            if not isinstance(ops_data, list):
                ops_data = [ops_data]
            ops_data = [
                item.model_dump() if isinstance(item, BaseModel) else item
                for item in ops_data
            ]
        except json.JSONDecodeError as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid JSON input: {str(e)}",
                    "total": 0,
                    "succeeded": 0,
                    "results": [],
                },
                indent=2,
            )

        results = []

        for i, spec in enumerate(ops_data):
            if not isinstance(spec, dict):
                results.append(
                    {
                        "index": i,
                        "success": False,
                        "error_code": "SESSION_OPERATION_INVALID",
                        "error": "Each session operation must be an object",
                        "retryable": False,
                    }
                )
                continue
            action = spec.get("action")

            if not action:
                results.append(
                    {
                        "index": i,
                        "success": False,
                        "error": "Missing 'action' field. Supported: "
                        + ", ".join(SESSION_DISPATCH.keys()),
                    }
                )
                continue

            action_lower = action.lower()
            dispatch_entry = SESSION_DISPATCH.get(action_lower)

            if not dispatch_entry:
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": f"Unknown action '{action}'. Supported: "
                        + ", ".join(SESSION_DISPATCH.keys()),
                    }
                )
                continue

            handler, required_fields = dispatch_entry

            field_error = _validate_required_fields(spec, required_fields, action_lower)
            if field_error:
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": field_error,
                    }
                )
                continue

            try:
                result = handler(spec)
                results.append({"index": i, "action": action_lower, **result})
            except CADConnectionError as e:
                logger.error(f"Connection error in op {i} ({action_lower}): {e}")
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": str(e),
                    }
                )
            except Exception as e:
                logger.error(f"Error in session op {i} ({action_lower}): {e}")
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": str(e),
                    }
                )

        return json.dumps(
            {
                "total": len(ops_data),
                "succeeded": sum(1 for r in results if r.get("success")),
                "results": results,
            },
            indent=2,
        )
