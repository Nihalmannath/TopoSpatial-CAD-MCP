"""
AutoCAD adapter for TopoSpatial-CAD MCP.

Implements CADInterface for AutoCAD using Windows COM.
Supports AutoCAD, ZWCAD, GstarCAD, and BricsCAD via factory pattern.

Refactored to use mixin classes for better organization and maintainability.
"""

import threading
import logging
from typing import Dict, Any

from core import (
    CADInterface,
    get_cad_config,
)
from .mixins import (
    UtilityMixin,
    ConnectionMixin,
    DrawingMixin,
    LayerMixin,
    FileMixin,
    ViewMixin,
    SelectionMixin,
    EntityMixin,
    ManipulationMixin,
    BlockMixin,
    ExportMixin,
    ArchitectureMixin,
    com_session,
    SelectionSetManager,
    com_safe,
)
from mcp_tools.constants import COLOR_MAP

logger = logging.getLogger(__name__)

# Export helpers for backward compatibility
__all__ = [
    "AutoCADAdapter",
    "com_session",
    "SelectionSetManager",
    "com_safe",
    "COLOR_MAP",
]


class AutoCADAdapter(
    UtilityMixin,
    ConnectionMixin,
    DrawingMixin,
    LayerMixin,
    FileMixin,
    ViewMixin,
    SelectionMixin,
    EntityMixin,
    ManipulationMixin,
    BlockMixin,
    ExportMixin,
    ArchitectureMixin,
    CADInterface,
):
    """Adapter for controlling AutoCAD via COM interface.

    [... docstring truncated for brevity ...]
    """

    def __init__(self, cad_type: str = "autocad"):
        """Initialize AutoCAD adapter.

        Args:
            cad_type: Type of CAD (autocad, zwcad, gcad, bricscad)
        """
        self.cad_type = cad_type.lower()
        self.config = get_cad_config(self.cad_type)

        self._application: Any = None
        self._document: Any = None
        self._aec_version: Optional[str] = None
        self._local = threading.local()

        self._drawing_state: Dict[str, Any] = {
            "entities": [],
            "current_layer": "0",
        }

    @property
    def application(self) -> Any:
        """Get the application COM proxy."""
        return self._application

    @application.setter
    def application(self, value: Any):
        """Set the application COM proxy."""
        self._application = value

    @property
    def document(self) -> Any:
        """Get the document COM proxy."""
        return self._document

    @document.setter
    def document(self, value: Any):
        """Set the document COM proxy."""
        self._document = value
