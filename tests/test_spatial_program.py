from __future__ import annotations

import pytest

from topology_engine.spatial_program import (
    SpatialProgram,
    diagnose_spatial_program,
    spatial_intent_id,
)


def _space(semantic_id: str, area: float = 14.0) -> dict:
    return {
        "@id": semantic_id,
        "@type": "top:Space",
        "rdfs:label": semantic_id,
        "cad:areaSquareMetres": area,
        "cad:properties": {},
    }


def test_spatial_program_validates_order_and_custom_type() -> None:
    SpatialProgram.model_validate(
        {
            "space_type": "bedroom",
            "area_targets_m2": {"minimum": 12, "target": 14, "maximum": 18},
        }
    )
    with pytest.raises(ValueError, match="minimum cannot exceed target"):
        SpatialProgram.model_validate(
            {"space_type": "bedroom", "area_targets_m2": {"minimum": 15, "target": 14}}
        )
    with pytest.raises(ValueError, match="custom_space_type"):
        SpatialProgram.model_validate({"space_type": "other"})


def test_spatial_intent_identity_is_symmetric_only_when_expected() -> None:
    assert spatial_intent_id("space:a", "space:b", "near") == spatial_intent_id(
        "space:b", "space:a", "near"
    )
    assert spatial_intent_id(
        "space:a", "space:b", "service_dependency"
    ) != spatial_intent_id("space:b", "space:a", "service_dependency")
    assert spatial_intent_id(
        "space:a", "space:b", "direct_access", "forward"
    ) != spatial_intent_id("space:b", "space:a", "direct_access", "forward")


def test_explicit_program_and_intent_diagnostics_are_advisory() -> None:
    bedroom = _space("space:bed", 10.0)
    bedroom["cad:properties"]["spatial_program"] = {
        "space_type": "bedroom",
        "area_targets_m2": {"minimum": 12, "target": 14},
        "environment": {"daylight": "required", "ventilation": "natural_preferred", "exterior_access": "none"},
    }
    corridor = _space("space:corridor")
    intent_id = spatial_intent_id("space:bed", "space:corridor", "direct_access")
    intent = {
        "@id": intent_id,
        "@type": "top:SpatialIntent",
        "cad:properties": {
            "intent_schema_version": 1,
            "source_space_id": "space:bed",
            "target_space_id": "space:corridor",
            "kind": "direct_access",
            "priority": "must",
            "direction": "bidirectional",
            "portal_requirement": "none",
            "evaluation_status": "unverified",
        },
    }
    issues = diagnose_spatial_program({"@graph": [bedroom, corridor, intent]})
    codes = {item["code"] for item in issues}
    assert "AREA_TARGET_BELOW_MINIMUM" in codes
    assert "ENVIRONMENTAL_REQUIREMENT_UNVERIFIED" in codes
    assert "REQUIRED_DIRECT_ACCESS_MISSING" in codes

