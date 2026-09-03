"""Data contracts for the optional topology subsystem."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ONTOLOGY_CLASSES = {
    "top:Room",
    "top:Wall",
    "top:Door",
    "top:Window",
    "top:Space",
    "top:Opening",
    "top:Connection",
    "top:SpatialIntent",
}


@dataclass(frozen=True)
class EntitySnapshot:
    """Thread-neutral representation of one CAD entity."""

    handle: str
    object_type: str
    layer: str
    geometry: Dict[str, Any]
    semantic: Dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> Dict[str, Any]:
        """Return stable JSON-compatible data used by drawing fingerprints."""
        return asdict(self)


@dataclass(frozen=True)
class DrawingSnapshot:
    """Pure-Python snapshot captured from one active CAD drawing."""

    drawing_name: str
    full_name: str
    units: str
    entities: List[EntitySnapshot]

    @property
    def revision(self) -> str:
        """Return a deterministic SHA-256 revision of geometry and semantics."""
        payload = {
            "drawing_name": self.drawing_name,
            "full_name": self.full_name,
            "units": self.units,
            "entities": [
                entity.canonical()
                for entity in sorted(self.entities, key=lambda item: item.handle)
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class TargetSpec(BaseModel):
    """Selector for an explicit annotation operation."""

    model_config = ConfigDict(extra="forbid")

    handles: List[str] = Field(default_factory=list)
    layer: Optional[str] = None
    candidate_id: Optional[str] = None
    grouping: Literal["single_object", "individual", "connected"] = "single_object"

    @model_validator(mode="after")
    def exactly_one_selector(self) -> "TargetSpec":
        selectors = bool(self.handles) + bool(self.layer) + bool(self.candidate_id)
        if selectors != 1:
            raise ValueError(
                "targets must specify exactly one of handles, layer, candidate_id"
            )
        return self


class TopologyChange(BaseModel):
    """One validated change in a topology preview document."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    op: Literal["annotate", "create", "update", "delete"]
    semantic_id: Optional[str] = Field(default=None, alias="@id")
    ontology_class: Optional[str] = Field(default=None, alias="@type")
    label: Optional[str] = None
    targets: Optional[TargetSpec] = None
    geometry: Optional[Dict[str, Any]] = None
    representation: Literal["auto", "native_aec", "standard"] = "auto"
    cascade: bool = False

    @model_validator(mode="after")
    def validate_operation_contract(self) -> "TopologyChange":
        if self.ontology_class and self.ontology_class not in ONTOLOGY_CLASSES:
            raise ValueError(
                f"Unsupported ontology class '{self.ontology_class}'. "
                f"Use one of: {', '.join(sorted(ONTOLOGY_CLASSES))}"
            )

        if self.op == "annotate":
            if self.targets is None or self.ontology_class is None:
                raise ValueError("annotate requires targets and @type")
            if self.geometry is not None:
                raise ValueError("annotate does not accept geometry")
        elif self.op == "create":
            if self.ontology_class is None or self.geometry is None:
                raise ValueError("create requires @type and geometry")
            if self.targets is not None:
                raise ValueError("create does not accept targets")
        elif self.op == "update":
            if self.semantic_id is None:
                raise ValueError("update requires @id")
            if self.geometry is None and self.label is None:
                raise ValueError("update requires geometry or label")
        elif self.op == "delete" and self.semantic_id is None:
            raise ValueError("delete requires @id")
        return self


class ChangeDocument(BaseModel):
    """Strict JSON-LD-shaped input accepted by preview."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    context: Dict[str, Any] = Field(alias="@context")
    base_revision: str
    changes: List[TopologyChange] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_context(self) -> "ChangeDocument":
        if self.context.get("top") != "http://w3id.org/topologicpy#":
            raise ValueError("@context.top must be 'http://w3id.org/topologicpy#'")
        return self


@dataclass
class PreviewTransaction:
    """Validated, non-mutating change set waiting to be applied."""

    transaction_id: str
    drawing_name: str
    base_revision: str
    created_at: float
    expires_at: float
    operations: List[Dict[str, Any]]
    diff: List[Dict[str, Any]]
    warnings: List[str]
    status: Literal["pending", "applied", "cancelled", "rolled_back"] = "pending"
    result: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
