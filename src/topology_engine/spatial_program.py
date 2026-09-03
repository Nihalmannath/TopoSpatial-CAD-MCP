"""Architect-entered space programs and deterministic spatial-intent checks."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


SpaceType = Literal[
    "living_room",
    "dining_room",
    "kitchen",
    "bedroom",
    "bathroom",
    "toilet",
    "corridor",
    "lobby",
    "staircase",
    "office",
    "storage",
    "utility",
    "balcony",
    "terrace",
    "parking",
    "outdoor",
    "other",
]
SpaceZone = Literal[
    "living", "sleeping", "service", "circulation", "office", "outdoor", "unassigned"
]
PrivacyLevel = Literal["public", "semi_public", "private", "service"]
DaylightRequirement = Literal["none", "preferred", "required"]
VentilationRequirement = Literal[
    "none", "mechanical", "natural_preferred", "natural_required"
]
ExteriorAccessRequirement = Literal["none", "preferred", "required"]
EgressRole = Literal["none", "primary", "secondary"]
AcousticSeparation = Literal["none", "low", "medium", "high"]
SpatialIntentKind = Literal[
    "adjacent",
    "direct_access",
    "near",
    "separated",
    "visual_connection",
    "acoustic_separation",
    "service_dependency",
    "sequence",
]
SpatialIntentPriority = Literal["must", "should", "prefer", "avoid", "must_not"]
SpatialIntentDirection = Literal["bidirectional", "forward", "reverse"]
PortalRequirement = Literal[
    "none", "existing_portal", "door", "opening", "any_portal"
]
IntentEvaluationStatus = Literal["satisfied", "unsatisfied", "unverified", "conflict"]


class AreaTargets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: Optional[float] = Field(default=None, ge=0)
    target: Optional[float] = Field(default=None, ge=0)
    maximum: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered_targets(self) -> "AreaTargets":
        values = [value for value in (self.minimum, self.target, self.maximum) if value is not None]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Area targets must be finite")
        if self.minimum is not None and self.target is not None and self.minimum > self.target:
            raise ValueError("area minimum cannot exceed target")
        if self.target is not None and self.maximum is not None and self.target > self.maximum:
            raise ValueError("area target cannot exceed maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("area minimum cannot exceed maximum")
        return self


class OccupancyProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    design_count: Optional[int] = Field(default=None, ge=0, le=10000)
    accessible_required: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_design_occupancy(cls, value: Any) -> Any:
        """Preserve version-2 profiles while standardising on design_count."""
        if isinstance(value, dict) and "design_occupancy" in value and "design_count" not in value:
            value = dict(value)
            value["design_count"] = value.pop("design_occupancy")
        return value


class EnvironmentProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daylight: DaylightRequirement = "none"
    ventilation: VentilationRequirement = "none"
    exterior_access: ExteriorAccessRequirement = "none"


class CirculationProgram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_entry: bool = False
    egress_role: EgressRole = "none"
    minimum_clear_width_mm: Optional[float] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def finite_width(self) -> "CirculationProgram":
        if self.minimum_clear_width_mm is not None and not math.isfinite(self.minimum_clear_width_mm):
            raise ValueError("minimum clear width must be finite")
        return self


class SpatialProgram(BaseModel):
    """Versioned architect-authored program stored below cad:properties."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    space_type: SpaceType
    custom_space_type: Optional[str] = Field(default=None, max_length=128)
    zone: SpaceZone = "unassigned"
    intended_use: str = Field(default="", max_length=500)
    area_targets_m2: AreaTargets = Field(default_factory=AreaTargets)
    occupancy: OccupancyProgram = Field(default_factory=OccupancyProgram)
    privacy: PrivacyLevel = "semi_public"
    environment: EnvironmentProgram = Field(default_factory=EnvironmentProgram)
    circulation: CirculationProgram = Field(default_factory=CirculationProgram)
    acoustic_separation: AcousticSeparation = "none"
    architect_notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_other_type(self) -> "SpatialProgram":
        custom = (self.custom_space_type or "").strip()
        if self.space_type == "other" and not custom:
            raise ValueError("custom_space_type is required when space_type is other")
        self.custom_space_type = custom or None
        self.intended_use = self.intended_use.strip()
        self.architect_notes = self.architect_notes.strip()
        return self


class SpatialIntentProperties(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_schema_version: Literal[1] = 1
    source_space_id: str = Field(min_length=1)
    target_space_id: str = Field(min_length=1)
    kind: SpatialIntentKind
    priority: SpatialIntentPriority
    direction: SpatialIntentDirection = "bidirectional"
    minimum_clear_width_mm: Optional[float] = Field(default=None, ge=0)
    portal_requirement: PortalRequirement = "none"
    rationale: str = Field(default="", max_length=1000)
    evaluation_status: IntentEvaluationStatus = "unverified"

    @model_validator(mode="after")
    def validate_intent(self) -> "SpatialIntentProperties":
        if self.source_space_id == self.target_space_id:
            raise ValueError("spatial intent endpoints must be different")
        if self.minimum_clear_width_mm is not None and not math.isfinite(self.minimum_clear_width_mm):
            raise ValueError("minimum clear width must be finite")
        self.rationale = self.rationale.strip()
        return self


SYMMETRIC_INTENTS = {
    "adjacent",
    "near",
    "separated",
    "visual_connection",
    "acoustic_separation",
}


def spatial_intent_id(
    source_space_id: str,
    target_space_id: str,
    kind: str,
    direction: str = "bidirectional",
) -> str:
    """Return the stable identity of one architect-authored relationship."""
    source = str(source_space_id).strip()
    target = str(target_space_id).strip()
    if kind in SYMMETRIC_INTENTS or (kind == "direct_access" and direction == "bidirectional"):
        source, target = sorted((source, target))
    digest = hashlib.sha256(f"1|{kind}|{source}|{target}".encode("utf-8")).hexdigest()[:20]
    return f"urn:topospatial:intent:{digest}"


def validate_semantic_node(node: Dict[str, Any]) -> None:
    """Validate editor-owned program and intent payloads without changing them."""
    label = node.get("rdfs:label")
    if label is not None:
        text = str(label).strip()
        if not text or len(text) > 128:
            raise ValueError("Space display name must contain 1 to 128 characters")
    properties = node.get("cad:properties", {})
    if not isinstance(properties, dict):
        raise ValueError("cad:properties must be an object")
    program = properties.get("spatial_program")
    if program is not None:
        SpatialProgram.model_validate(program)
    if node.get("@type") == "top:SpatialIntent":
        intent = SpatialIntentProperties.model_validate(properties)
        expected = spatial_intent_id(
            intent.source_space_id,
            intent.target_space_id,
            intent.kind,
            intent.direction,
        )
        if str(node.get("@id")) != expected:
            raise ValueError(f"Spatial intent ID must be '{expected}'")


def diagnose_spatial_program(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Evaluate explicit space programs and intents without claiming code compliance."""
    nodes = {str(node.get("@id")): node for node in graph.get("@graph", [])}
    issues: List[Dict[str, Any]] = []
    adjacency = _relation_pairs(nodes, "top:adjacentTo")
    connections = _connection_pairs(nodes)

    for semantic_id, node in sorted(nodes.items()):
        if node.get("@type") in {"top:Room", "top:Space"}:
            program_data = node.get("cad:properties", {}).get("spatial_program")
            if program_data is not None:
                try:
                    program = SpatialProgram.model_validate(program_data)
                except ValueError as exc:
                    issues.append(_issue("SPATIAL_PROGRAM_INVALID", "error", str(exc), [semantic_id]))
                    continue
                area = _area_m2(node)
                if area is not None and program.area_targets_m2.minimum is not None and area < program.area_targets_m2.minimum:
                    issues.append(_issue("AREA_TARGET_BELOW_MINIMUM", "error", "Space area is below its explicit program minimum.", [semantic_id]))
                if area is not None and program.area_targets_m2.maximum is not None and area > program.area_targets_m2.maximum:
                    issues.append(_issue("AREA_TARGET_ABOVE_MAXIMUM", "warning", "Space area is above its explicit program maximum.", [semantic_id]))
                if program.environment.daylight != "none" or program.environment.ventilation in {"natural_preferred", "natural_required"}:
                    if not _has_environmental_evidence(node, nodes):
                        issues.append(_issue("ENVIRONMENTAL_REQUIREMENT_UNVERIFIED", "info", "The stated daylight or natural-ventilation requirement has no verified facade/window evidence.", [semantic_id]))

        if node.get("@type") != "top:SpatialIntent":
            continue
        try:
            intent = SpatialIntentProperties.model_validate(node.get("cad:properties", {}))
        except ValueError as exc:
            issues.append(_issue("SPATIAL_INTENT_INVALID", "error", str(exc), [semantic_id]))
            continue
        if intent.source_space_id not in nodes or intent.target_space_id not in nodes:
            issues.append(_issue("SPATIAL_INTENT_ENDPOINT_MISSING", "error", "Spatial intent references a missing space.", [semantic_id, intent.source_space_id, intent.target_space_id]))
            continue
        pair = tuple(sorted((intent.source_space_id, intent.target_space_id)))
        present = pair in adjacency if intent.kind == "adjacent" else pair in connections if intent.kind == "direct_access" else None
        violation = False
        code = ""
        if intent.kind == "adjacent" and intent.priority in {"must", "should", "prefer"} and not present:
            code, violation = "REQUIRED_ADJACENCY_MISSING", True
        elif intent.kind == "adjacent" and intent.priority in {"avoid", "must_not"} and present:
            code, violation = "FORBIDDEN_ADJACENCY_PRESENT", True
        elif intent.kind == "direct_access" and intent.priority in {"must", "should", "prefer"} and not present:
            code, violation = "REQUIRED_DIRECT_ACCESS_MISSING", True
        elif intent.kind == "direct_access" and intent.priority in {"avoid", "must_not"} and present:
            code, violation = "FORBIDDEN_DIRECT_ACCESS_PRESENT", True
        if violation:
            severity = "error" if intent.priority in {"must", "must_not"} else "warning" if intent.priority == "should" else "info"
            issues.append(_issue(code, severity, "The current topology does not satisfy the explicit spatial intent.", [semantic_id, intent.source_space_id, intent.target_space_id]))
    return sorted(issues, key=lambda item: (item["code"], item["affected"]))


def diagnose_graph(graph: Dict[str, Any], required_width_mm: float = 1200.0) -> List[Dict[str, Any]]:
    """Combine circulation and architect-authored program diagnostics."""
    from topology_engine.navigation import diagnose_connectivity

    return sorted(
        diagnose_connectivity(graph, required_width_mm) + diagnose_spatial_program(graph),
        key=lambda item: (item["code"], item["affected"]),
    )


def _issue(code: str, severity: str, message: str, affected: List[str]) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, "affected": sorted(set(affected))}


def _relation_pairs(nodes: Dict[str, Dict[str, Any]], predicate: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for source, node in nodes.items():
        for value in node.get(predicate, []):
            target = str(value.get("@id", "")) if isinstance(value, dict) else str(value)
            if target:
                pairs.add(tuple(sorted((source, target))))
    return pairs


def _connection_pairs(nodes: Dict[str, Dict[str, Any]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for node in nodes.values():
        if node.get("@type") != "top:Connection" or node.get("cad:geometry", {}).get("status", "confirmed") != "confirmed":
            continue
        geometry = node.get("cad:geometry", {})
        source = str(geometry.get("from_space_id", ""))
        target = str(geometry.get("to_space_id", ""))
        if source and target:
            pairs.add(tuple(sorted((source, target))))
    return pairs


def _area_m2(node: Dict[str, Any]) -> Optional[float]:
    value = node.get("cad:areaSquareMetres", node.get("top:hasArea"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _has_environmental_evidence(node: Dict[str, Any], nodes: Dict[str, Dict[str, Any]]) -> bool:
    room_id = str(node.get("@id"))
    for candidate in nodes.values():
        if candidate.get("@type") not in {"top:Window", "top:Opening"}:
            continue
        rooms = candidate.get("cad:boundingRooms", [])
        if room_id in rooms:
            return True
    return False
