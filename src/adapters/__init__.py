"""
CAD adapters for TopoSpatial-CAD MCP.

Design:
- All compatible CADs (AutoCAD, ZWCAD, GstarCAD, BricsCAD) use the same COM API
- Single AutoCADAdapter works with all CAD types via ProgID configuration in config.json
- No factory pattern needed - just instantiate AutoCADAdapter(cad_type)
"""

from .autocad_adapter import AutoCADAdapter
from .com_worker import (
    COMWorker,
    get_com_worker,
    run_com,
    is_com_busy_error,
    is_com_not_running_error,
    CADBusyCircuitBreaker,
)

__all__ = [
    "AutoCADAdapter",
    "COMWorker",
    "get_com_worker",
    "run_com",
    "is_com_busy_error",
    "is_com_not_running_error",
    "CADBusyCircuitBreaker",
]
