"""Native AutoCAD Architecture (ACA) object automation.

This module intentionally uses ACA's late-bound COM surface instead of ribbon
commands.  It keeps product-specific behavior behind an adapter capability
boundary so ordinary AutoCAD, ZWCAD, GstarCAD, and BricsCAD retain the existing
standard-entity implementation.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

if sys.platform == "win32":
    import pythoncom
else:
    from unittest.mock import MagicMock
    pythoncom = MagicMock()

logger = logging.getLogger(__name__)


class ArchitectureMixin:
    """Capability detection and native ACA wall/opening creation."""

    if TYPE_CHECKING:
        cad_type: str
        _local: Any

        def _get_application(self, operation: str = "operation") -> Any: ...

        def _get_document(self, operation: str = "operation") -> Any: ...

        def _to_variant_array(self, point: Iterable[float]) -> Any: ...

    _AEC_APPLICATION_PREFIX = "AecX.AecArchBaseApplication."
    _AEC_DATABASE_PREFIX = "AecX.AecArchBaseDatabase."
    _AEC_OPENING_ANCHOR_PREFIX = "AecX.AecAnchorOpeningBaseToWall."

    @staticmethod
    def _version_key(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split(".") if part.isdigit())

    @classmethod
    def _registered_aec_versions(cls) -> List[str]:
        """Return installed AEC automation suffixes, newest first."""
        try:
            import winreg

            versions: set[str] = set()
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(count):
                    name = winreg.EnumKey(root, index)
                    if name.startswith(cls._AEC_APPLICATION_PREFIX):
                        suffix = name[len(cls._AEC_APPLICATION_PREFIX) :]
                        if re.fullmatch(r"\d+(?:\.\d+)+", suffix):
                            versions.add(suffix)
            discovered = sorted(versions, key=cls._version_key, reverse=True)
            return discovered or ["8.7", "8.6"]
        except Exception:
            logger.debug("Could not enumerate registered AEC versions", exc_info=True)
            # Known recent versions are only a fallback; registry discovery is
            # the normal path and prevents coupling to a particular ACA release.
            return ["8.7", "8.6"]

    def _discover_aec_version(self, refresh: bool = False) -> Optional[str]:
        if self.cad_type != "autocad":
            return None
        cached = getattr(self, "_aec_version", None)
        if cached is None and hasattr(self, "_local"):
            cached = getattr(self._local, "aec_version", None)
        if cached is not None and not refresh:
            return cached or None

        application = self._get_application("detect AutoCAD Architecture")
        try:
            loaded = any(
                "AECARCHBASE" in str(module).upper()
                for module in application.ListArx()
            )
        except Exception:
            loaded = "ARCHITECTURE" in str(
                getattr(application, "Caption", "")
            ).upper()
        if not loaded:
            self._aec_version = ""
            if hasattr(self, "_local"):
                self._local.aec_version = ""
            return None

        for version in self._registered_aec_versions():
            try:
                application.GetInterfaceObject(
                    f"{self._AEC_APPLICATION_PREFIX}{version}"
                )
                self._aec_version = version
                if hasattr(self, "_local"):
                    self._local.aec_version = version
                return version
            except Exception:
                continue
        self._aec_version = ""
        if hasattr(self, "_local"):
            self._local.aec_version = ""
        return None

    def _aec_database(self, version: str) -> Any:
        application = self._get_application("access ACA styles")
        document = self._get_document("access ACA styles")
        database = application.GetInterfaceObject(
            f"{self._AEC_DATABASE_PREFIX}{version}"
        )
        database.Init(document.Database)
        return database

    @staticmethod
    def _collection_names(collection: Any) -> List[str]:
        names = []
        for index in range(int(collection.Count)):
            names.append(str(collection.Item(index).Name))
        return sorted(set(names), key=str.casefold)

    def get_architecture_capabilities(
        self, include_styles: bool = True, refresh: bool = False
    ) -> Dict[str, Any]:
        """Describe native architectural authoring available in this session."""
        application = self._get_application("inspect architecture capabilities")
        version = self._discover_aec_version(refresh=refresh)
        result: Dict[str, Any] = {
            "cad_type": self.cad_type,
            "application": str(getattr(application, "Name", self.cad_type)),
            "application_version": str(getattr(application, "Version", "")),
            "caption": str(getattr(application, "Caption", "")),
            "native_aec": version is not None,
            "aec_api_version": version,
            "preferred_representation": "native_aec" if version else "standard",
            "fallback_representation": "standard",
            "supported_objects": ["wall", "door", "window"] if version else [],
        }
        if version and include_styles:
            try:
                database = self._aec_database(version)
                result["styles"] = {
                    "wall": self._collection_names(database.WallStyles),
                    "door": self._collection_names(database.DoorStyles),
                    "window": self._collection_names(database.WindowStyles),
                }
            except Exception as exc:
                result["styles"] = {}
                result["style_warning"] = str(exc)
        if not version:
            result["reason"] = (
                "The active product does not expose the AutoCAD Architecture "
                "automation libraries"
            )
        return result

    def _require_aec(self) -> str:
        version = self._discover_aec_version()
        if version is None:
            raise RuntimeError(
                "Native AEC objects require an active AutoCAD Architecture session"
            )
        return version

    @staticmethod
    def _set_com_property(target: Any, name: str, value: Any) -> None:
        """Set an ACA property with bounded retry for RPC_E_CALL_REJECTED."""
        last_error: Optional[Exception] = None
        for attempt in range(6):
            try:
                setattr(target, name, value)
                return
            except Exception as exc:
                last_error = exc
                if attempt < 5:
                    time.sleep(0.15 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _add_custom_object(self, object_name: str) -> Any:
        document = self._get_document(f"create native {object_name}")
        last_error: Optional[Exception] = None
        for attempt in range(6):
            try:
                return document.ModelSpace.AddCustomObject(object_name)
            except Exception as exc:
                last_error = exc
                if attempt < 5:
                    time.sleep(0.15 * (attempt + 1))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _invoke_attach_entity(anchor: Any, entity: Any) -> None:
        """Invoke ACA's late-bound AttachEntity(IDispatch) signature.

        pywin32 cannot infer this parameter type from ACA's dynamic wrapper, so
        a normal ``anchor.AttachEntity(entity)`` raises MEMBER_NOT_FOUND.
        """
        dispid = anchor._oleobj_.GetIDsOfNames("AttachEntity")
        anchor._oleobj_.InvokeTypes(
            dispid,
            0,
            pythoncom.DISPATCH_METHOD,
            (pythoncom.VT_EMPTY, 0),
            ((pythoncom.VT_DISPATCH, 0),),
            entity,
        )

    def create_native_wall(
        self,
        start: Iterable[float],
        end: Iterable[float],
        width: float,
        height: float = 3000.0,
        style: str = "Standard",
        layer: str = "AI-WALLS",
    ) -> Dict[str, str]:
        """Create one native ``AecDbWall`` on its centerline."""
        self._require_aec()
        wall = self._add_custom_object("AecWall")
        try:
            self._set_com_property(wall, "StartPoint", self._to_variant_array(start))
            self._set_com_property(wall, "EndPoint", self._to_variant_array(end))
            self._set_com_property(wall, "Width", float(width))
            self._set_com_property(wall, "BaseHeight", float(height))
            self._set_com_property(wall, "StyleName", str(style))
            self._set_com_property(wall, "Layer", layer)
            wall.Update()
            return {
                "handle": str(wall.Handle),
                "object_type": str(wall.ObjectName),
                "representation": "native_aec",
            }
        except Exception:
            try:
                wall.Delete()
            except Exception:
                pass
            raise

    def create_native_opening(
        self,
        opening_type: str,
        host_handle: str,
        offset: float,
        width: float,
        height: float,
        style: str = "Standard",
        layer: str = "0",
        sill_height: float = 0.0,
        hinge: str = "left",
        swing: str = "in",
        swing_angle: float = 90.0,
    ) -> Dict[str, str]:
        """Create a native door/window and anchor it to an ``AecDbWall``."""
        version = self._require_aec()
        if opening_type not in {"door", "window"}:
            raise ValueError("opening_type must be 'door' or 'window'")
        document = self._get_document(f"create native {opening_type}")
        host = document.HandleToObject(host_handle)
        if str(host.ObjectName).upper() != "AECDBWALL":
            raise ValueError(
                f"Native {opening_type} host must be AecDbWall, got {host.ObjectName}"
            )

        entity = self._add_custom_object(
            "AecDoor" if opening_type == "door" else "AecWindow"
        )
        try:
            self._set_com_property(entity, "Width", float(width))
            self._set_com_property(entity, "Height", float(height))
            self._set_com_property(entity, "StyleName", str(style))
            self._set_com_property(entity, "Layer", layer)
            if opening_type == "door":
                self._set_com_property(entity, "SwingAngle", float(swing_angle))
            else:
                self._set_com_property(entity, "SillHeight", float(sill_height))

            application = self._get_application(f"anchor native {opening_type}")
            anchor = application.GetInterfaceObject(
                f"{self._AEC_OPENING_ANCHOR_PREFIX}{version}"
            )
            # Offset is measured from the wall start to the opening's start edge.
            self._set_com_property(anchor, "Reference", host)
            self._set_com_property(anchor, "XPositionFrom", 0)
            self._set_com_property(anchor, "XPositionTo", 0)
            self._set_com_property(anchor, "XDistance", float(offset))
            self._set_com_property(anchor, "FlipX", hinge == "right")
            self._set_com_property(anchor, "FlipY", swing == "out")
            self._invoke_attach_entity(anchor, entity)
            entity.Update()
            return {
                "handle": str(entity.Handle),
                "object_type": str(entity.ObjectName),
                "representation": "native_aec",
                "host_handle": str(host.Handle),
            }
        except Exception:
            try:
                entity.Delete()
            except Exception:
                pass
            raise
