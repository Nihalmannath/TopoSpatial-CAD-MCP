"""
CAD Bridge Module for TopoSpatial-CAD MCP Bundle
Connects the MCP server to local AutoCAD / CAD COM sessions.
"""

import sys
import logging
from typing import Optional, Any, Dict

logger = logging.getLogger("topospatial.cad_bridge")


def get_active_cad_adapter() -> Optional[Any]:
    """Retrieve the active CAD COM adapter."""
    try:
        from adapters.adapter_manager import get_adapter
        return get_adapter(only_if_running=True)
    except Exception as exc:
        logger.debug(f"Could not connect to CAD adapter: {exc}")
        return None


def get_bridge_status() -> Dict[str, Any]:
    """Get compact CAD connection status for diagnostic reporting."""
    from server.doctor import check_autocad_process, check_cad_com_connection, check_active_document
    
    proc = check_autocad_process()
    com = check_cad_com_connection()
    doc = check_active_document(com)
    
    return {
        "cad_process_running": proc["passed"],
        "cad_com_connected": com["passed"],
        "drawing_active": doc["passed"],
        "details": {
            "process": proc["detail"],
            "connection": com["detail"],
            "document": doc["detail"]
        }
    }
