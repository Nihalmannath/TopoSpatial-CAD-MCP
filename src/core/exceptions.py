"""
Custom exceptions for TopoSpatial-CAD MCP server.
Provides domain-specific error handling for CAD operations and spatial topology.
"""

from typing import Any, List


class TopoSpatialError(Exception):
    """Base exception for all TopoSpatial-CAD MCP errors."""

    pass


# Backward-compatible alias
MultiCADError = TopoSpatialError


class CADConnectionError(TopoSpatialError):
    """Raised when connection to CAD application fails."""

    def __init__(self, cad_type: str, reason: str):
        self.cad_type = cad_type
        self.reason = reason
        super().__init__(f"Failed to connect to {cad_type}: {reason}")


class CADOperationError(TopoSpatialError):
    """Raised when a CAD operation fails."""

    def __init__(self, operation: str, reason: str):
        self.operation = operation
        self.reason = reason
        super().__init__(f"CAD operation '{operation}' failed: {reason}")


class InvalidParameterError(TopoSpatialError):
    """Raised when invalid parameters are provided."""

    def __init__(self, param_name: str, param_value: Any, expected_type: str):
        self.param_name = param_name
        self.param_value = param_value
        self.expected_type = expected_type
        super().__init__(
            f"Invalid parameter '{param_name}': got {type(param_value).__name__}, "
            f"expected {expected_type}"
        )


class CoordinateError(InvalidParameterError):
    """Raised when coordinate validation fails."""

    def __init__(self, coordinate: Any, reason: str):
        super().__init__("coordinate", coordinate, "tuple/list of 2-3 numbers")
        self.reason = reason


class ColorError(InvalidParameterError):
    """Raised when color specification is invalid."""

    def __init__(self, color: str, reason: str):
        super().__init__("color", color, "valid color name or hex code")
        self.reason = reason


class LayerError(TopoSpatialError):
    """Raised when layer operations fail."""

    def __init__(self, layer_name: str, reason: str):
        self.layer_name = layer_name
        self.reason = reason
        super().__init__(f"Layer operation on '{layer_name}' failed: {reason}")


class CADNotSupportedError(TopoSpatialError):
    """Raised when requested CAD application is not supported."""

    def __init__(self, cad_type: str, supported: List[str]):
        self.cad_type = cad_type
        self.supported = supported
        super().__init__(
            f"CAD type '{cad_type}' is not supported. Supported: {', '.join(supported)}"
        )


class ConfigError(TopoSpatialError):
    """Raised when configuration is invalid or missing."""

    def __init__(self, config_file: str, reason: str):
        self.config_file = config_file
        self.reason = reason
        super().__init__(f"Configuration error in '{config_file}': {reason}")
