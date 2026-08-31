"""Tests for capability-based AutoCAD Architecture automation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adapters import AutoCADAdapter
from topology_engine.cad_bridge import CADTopologyBridge


def _named_collection(*names: str) -> MagicMock:
    collection = MagicMock()
    collection.Count = len(names)
    collection.Item.side_effect = [SimpleNamespace(Name=name) for name in names]
    return collection


def test_capabilities_discover_native_aec_and_styles() -> None:
    adapter = AutoCADAdapter("autocad")
    application = MagicMock()
    application.Name = "AutoCAD"
    application.Version = "24.3s"
    application.Caption = "AutoCAD Architecture 2024"
    application.ListArx.return_value = ["AecArchBase.dbx"]
    database = MagicMock()
    database.WallStyles = _named_collection("Standard", "CMU-200")
    database.DoorStyles = _named_collection("Standard")
    database.WindowStyles = _named_collection("Standard")
    application.GetInterfaceObject.side_effect = [MagicMock(), database]
    document = SimpleNamespace(Database=MagicMock())

    with (
        patch.object(adapter, "_get_application", return_value=application),
        patch.object(adapter, "_get_document", return_value=document),
        patch.object(adapter, "_registered_aec_versions", return_value=["8.6"]),
    ):
        result = adapter.get_architecture_capabilities()

    assert result["native_aec"] is True
    assert result["aec_api_version"] == "8.6"
    assert result["styles"]["wall"] == ["CMU-200", "Standard"]
    assert result["supported_objects"] == ["wall", "door", "window"]
    database.Init.assert_called_once_with(document.Database)


def test_non_autocad_adapter_reports_no_native_aec() -> None:
    adapter = AutoCADAdapter("bricscad")
    application = SimpleNamespace(Name="BricsCAD", Version="25", Caption="BricsCAD")

    with patch.object(adapter, "_get_application", return_value=application):
        result = adapter.get_architecture_capabilities(include_styles=False)

    assert result["native_aec"] is False
    assert result["preferred_representation"] == "standard"


def test_create_native_wall_sets_modern_aca_properties() -> None:
    adapter = AutoCADAdapter("autocad")
    wall = MagicMock()
    wall.Handle = "A1"
    wall.ObjectName = "AecDbWall"

    with (
        patch.object(adapter, "_require_aec", return_value="8.6"),
        patch.object(adapter, "_add_custom_object", return_value=wall),
        patch.object(adapter, "_to_variant_array", side_effect=lambda value: tuple(value)),
    ):
        result = adapter.create_native_wall(
            (0, 0, 0),
            (5000, 0, 0),
            width=200,
            height=3000,
            style="Standard",
            layer="AI-WALLS",
        )

    assert wall.StartPoint == (0, 0, 0)
    assert wall.EndPoint == (5000, 0, 0)
    assert wall.Width == 200.0
    assert wall.BaseHeight == 3000.0
    assert wall.StyleName == "Standard"
    assert wall.Layer == "AI-WALLS"
    assert result == {
        "handle": "A1",
        "object_type": "AecDbWall",
        "representation": "native_aec",
    }


def test_native_opening_requires_a_native_wall_host() -> None:
    adapter = AutoCADAdapter("autocad")
    host = SimpleNamespace(ObjectName="AcDbPolyline")
    document = MagicMock()
    document.HandleToObject.return_value = host

    with (
        patch.object(adapter, "_require_aec", return_value="8.6"),
        patch.object(adapter, "_get_document", return_value=document),
    ):
        with pytest.raises(ValueError, match="AecDbWall"):
            adapter.create_native_opening(
                "door", "A1", offset=1000, width=900, height=2100
            )


def test_representation_selection_is_capability_based() -> None:
    adapter = MagicMock()
    adapter.get_architecture_capabilities.return_value = {"native_aec": True}

    assert CADTopologyBridge._select_representation(adapter, "auto") == "native_aec"
    assert CADTopologyBridge._select_representation(adapter, "standard") == "standard"

    adapter.get_architecture_capabilities.return_value = {"native_aec": False}
    assert CADTopologyBridge._select_representation(adapter, "auto") == "standard"
    with pytest.raises(ValueError, match="requires AutoCAD Architecture"):
        CADTopologyBridge._select_representation(adapter, "native_aec")


def test_native_opening_falls_back_for_standard_host_in_auto_mode() -> None:
    adapter = MagicMock()
    adapter.get_architecture_capabilities.return_value = {"native_aec": True}
    wall = {
        "representation": "standard",
        "_object_type": "AcDbPolyline",
    }

    assert (
        CADTopologyBridge._select_opening_representation(adapter, "auto", wall)
        == "standard"
    )
    with pytest.raises(ValueError, match="native AecDbWall host"):
        CADTopologyBridge._select_opening_representation(
            adapter, "native_aec", wall
        )


def test_preview_resolves_auto_representation_for_native_wall_and_opening() -> None:
    adapter = MagicMock()
    adapter.get_architecture_capabilities.return_value = {"native_aec": True}
    operations = [
        {
            "kind": "create_managed",
            "semantic_id": "urn:wall:1",
            "ontology_class": "top:Wall",
            "representation": "auto",
            "geometry": {"start": [0, 0], "end": [5000, 0]},
        },
        {
            "kind": "create_managed",
            "semantic_id": "urn:door:1",
            "ontology_class": "top:Door",
            "representation": "auto",
            "geometry": {"host_wall_id": "urn:wall:1", "offset": 1000},
        },
    ]

    CADTopologyBridge().resolve_operation_representations(
        adapter, operations, {"graph": {"@graph": []}}
    )

    assert [item["representation"] for item in operations] == [
        "native_aec",
        "native_aec",
    ]


def test_annotated_native_wall_can_host_native_opening_in_same_preview() -> None:
    adapter = MagicMock()
    adapter.get_architecture_capabilities.return_value = {"native_aec": True}
    operations = [
        {
            "kind": "annotate_handles",
            "semantic_id": "urn:wall:existing",
            "ontology_class": "top:Wall",
            "representation": "native_aec",
            "handles": ["A1"],
        },
        {
            "kind": "create_managed",
            "semantic_id": "urn:window:1",
            "ontology_class": "top:Window",
            "representation": "auto",
            "geometry": {
                "host_wall_id": "urn:wall:existing",
                "offset": 2500,
            },
        },
    ]

    CADTopologyBridge().resolve_operation_representations(
        adapter, operations, {"graph": {"@graph": []}}
    )

    assert operations[1]["representation"] == "native_aec"


def test_preview_rejects_unknown_native_aec_style() -> None:
    adapter = MagicMock()
    adapter.get_architecture_capabilities.side_effect = [
        {"native_aec": True},
        {"native_aec": True, "styles": {"wall": ["Standard"]}},
    ]
    operations = [
        {
            "kind": "create_managed",
            "semantic_id": "urn:wall:1",
            "ontology_class": "top:Wall",
            "representation": "auto",
            "geometry": {
                "start": [0, 0],
                "end": [5000, 0],
                "style": "Missing Style",
            },
        }
    ]

    with pytest.raises(ValueError, match="Unknown ACA wall style"):
        CADTopologyBridge().resolve_operation_representations(
            adapter, operations, {"graph": {"@graph": []}}
        )
