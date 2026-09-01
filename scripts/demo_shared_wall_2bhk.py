"""Create and verify a native-AEC 2BHK shared-wall demonstration drawing."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pythoncom
from pydantic import TypeAdapter

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from adapters.adapter_manager import get_adapter  # noqa: E402
from design_engine.models import DesignRequest  # noqa: E402
from design_engine.orchestrator import DesignOrchestrator  # noqa: E402

REQUEST_ADAPTER = TypeAdapter(DesignRequest)
ROOM_SIZE_MM = 4000.0
WALL_THICKNESS_MM = 200.0
ROOM_PITCH_MM = ROOM_SIZE_MM + WALL_THICKNESS_MM


def room(room_id: str, label: str, x: float, y: float) -> dict[str, Any]:
    """Build one rectangular room change using the clear-interior contract."""
    return {
        "op": "create",
        "@id": room_id,
        "@type": "top:Room",
        "label": label,
        "representation": "auto",
        "geometry": {
            "origin": [x, y],
            "clear_width": ROOM_SIZE_MM,
            "clear_depth": ROOM_SIZE_MM,
            "wall_thickness": WALL_THICKNESS_MM,
            "wall_height": 3000.0,
            "wall_style": "Standard",
        },
    }


def door(
    door_id: str,
    label: str,
    host_wall_id: str,
    offset: float,
    width: float = 900.0,
) -> dict[str, Any]:
    """Build one door change hosted by a room-wall compatibility alias."""
    return {
        "op": "create",
        "@id": door_id,
        "@type": "top:Door",
        "label": label,
        "representation": "auto",
        "geometry": {
            "host_wall_id": host_wall_id,
            "offset": offset,
            "width": width,
            "height": 2100.0,
            "style": "Standard",
            "hinge": "left",
            "swing": "in",
            "swing_angle_deg": 90.0,
        },
    }


def window(
    window_id: str,
    label: str,
    host_wall_id: str,
    offset: float,
    width: float = 1400.0,
) -> dict[str, Any]:
    """Build one window change hosted by a room-wall compatibility alias."""
    return {
        "op": "create",
        "@id": window_id,
        "@type": "top:Window",
        "label": label,
        "representation": "auto",
        "geometry": {
            "host_wall_id": host_wall_id,
            "offset": offset,
            "width": width,
            "height": 1200.0,
            "sill_height": 900.0,
            "style": "Standard",
        },
    }


def design_changes() -> list[dict[str, Any]]:
    """Return one approved semantic batch for a compact 2BHK plan."""
    return [
        room("urn:demo:room:living", "LIVING / DINING", 0.0, 0.0),
        room("urn:demo:room:kitchen", "KITCHEN", ROOM_PITCH_MM, 0.0),
        room("urn:demo:room:bedroom-1", "BEDROOM 1", 0.0, ROOM_PITCH_MM),
        room(
            "urn:demo:room:bedroom-2",
            "BEDROOM 2",
            ROOM_PITCH_MM,
            ROOM_PITCH_MM,
        ),
        door(
            "urn:demo:door:entry",
            "MAIN ENTRY",
            "urn:demo:room:living:wall:1",
            1700.0,
            1000.0,
        ),
        door(
            "urn:demo:door:kitchen",
            "KITCHEN DOOR",
            "urn:demo:room:living:wall:2",
            1400.0,
        ),
        door(
            "urn:demo:door:bedroom-1",
            "BEDROOM 1 DOOR",
            "urn:demo:room:living:wall:3",
            2500.0,
        ),
        door(
            "urn:demo:door:bedroom-2",
            "BEDROOM 2 DOOR",
            "urn:demo:room:kitchen:wall:3",
            2300.0,
        ),
        window(
            "urn:demo:window:living",
            "LIVING WINDOW",
            "urn:demo:room:living:wall:4",
            1400.0,
        ),
        window(
            "urn:demo:window:kitchen",
            "KITCHEN WINDOW",
            "urn:demo:room:kitchen:wall:1",
            1500.0,
        ),
        window(
            "urn:demo:window:bedroom-1",
            "BEDROOM 1 WINDOW",
            "urn:demo:room:bedroom-1:wall:3",
            1500.0,
        ),
        window(
            "urn:demo:window:bedroom-2",
            "BEDROOM 2 WINDOW",
            "urn:demo:room:bedroom-2:wall:3",
            1500.0,
        ),
    ]


def add_annotations(adapter: Any) -> None:
    """Add one batched visual layer after the semantic transaction succeeds."""
    adapter.create_layer("A-ANNO-MCP", "green", 18)
    annotations = [
        ((1050.0, 2050.0, 0.0), "LIVING / DINING", 260.0),
        ((5500.0, 2050.0, 0.0), "KITCHEN", 260.0),
        ((1150.0, 6250.0, 0.0), "BEDROOM 1", 260.0),
        ((5350.0, 6250.0, 0.0), "BEDROOM 2", 260.0),
        ((600.0, 8750.0, 0.0), "MCP 2BHK - NATIVE AEC SHARED-WALL DEMO", 260.0),
        (
            (600.0, -800.0, 0.0),
            "4 ROOMS | 12 PHYSICAL WALLS | 4 SHARED PARTITIONS | NO DUPLICATES",
            180.0,
        ),
    ]
    for position, text, height in annotations:
        adapter.draw_text(
            position,
            text,
            height=height,
            layer="A-ANNO-MCP",
            color="green",
            _skip_refresh=True,
        )
    adapter.refresh_view()


def run() -> dict[str, Any]:
    """Preview, apply, verify, save, and capture the complete demonstration."""
    pythoncom.CoInitialize()
    try:
        adapter = get_adapter(only_if_running=True)
        capabilities = adapter.get_architecture_capabilities(include_styles=True)
        if not capabilities.get("native_aec"):
            raise RuntimeError(
                "This demo requires AutoCAD Architecture native AEC support"
            )

        if not adapter.new_drawing():
            raise RuntimeError("AutoCAD could not create the requested demo drawing")
        document = adapter.document
        document.SetVariable("INSUNITS", 4)
        document.SetVariable("LUNITS", 2)
        document.SetVariable("LUPREC", 0)

        filename = datetime.now().strftime(
            "MCP_Shared_Wall_2BHK_Demo_%Y%m%d_%H%M%S.dwg"
        )
        if not adapter.save_drawing(filename=filename):
            raise RuntimeError("Could not save the new demo drawing before preview")

        orchestrator = DesignOrchestrator()
        inspected = orchestrator.execute(
            REQUEST_ADAPTER.validate_python(
                {"action": "inspect", "detail_level": "summary"}
            ),
            adapter,
        )
        if not inspected.get("success"):
            raise RuntimeError(f"Initial inspect failed: {inspected}")

        preview = orchestrator.execute(
            REQUEST_ADAPTER.validate_python(
                {
                    "action": "preview",
                    "task_id": "shared-wall-2bhk-demo",
                    "base_revision": inspected["revision"],
                    "changes": design_changes(),
                    "detail_level": "debug",
                }
            ),
            adapter,
        )
        if not preview.get("success"):
            raise RuntimeError(f"Preview failed: {preview}")

        wall_operations = [
            operation
            for operation in preview["operations"]
            if operation.get("ontology_class") == "top:Wall"
        ]
        shared_preview_walls = [
            operation
            for operation in wall_operations
            if len(operation.get("bounding_room_ids", [])) == 2
        ]
        if len(wall_operations) != 12 or len(shared_preview_walls) != 4:
            raise RuntimeError(
                "Normalized preview did not produce the expected 12-wall/4-shared plan"
            )

        applied = orchestrator.execute(
            REQUEST_ADAPTER.validate_python(
                {
                    "action": "apply",
                    "task_id": "shared-wall-2bhk-demo",
                    "transaction_id": preview["transaction_id"],
                    "detail_level": "detailed",
                }
            ),
            adapter,
        )
        if not applied.get("success") or applied.get("status") != "applied":
            raise RuntimeError(f"Apply failed: {applied}")

        verified = orchestrator.execute(
            REQUEST_ADAPTER.validate_python(
                {
                    "action": "inspect",
                    "task_id": "shared-wall-2bhk-demo",
                    "detail_level": "debug",
                    "budget": {
                        "max_entities": 100,
                        "max_neighbors": 20,
                        "graph_depth": 1,
                        "include_geometry": False,
                        "include_metadata": True,
                    },
                }
            ),
            adapter,
        )
        semantic_walls = [
            node
            for node in verified.get("semantic_index", [])
            if node.get("class") == "top:Wall"
        ]
        shared_semantic_walls = [
            node
            for node in semantic_walls
            if len(node.get("relationships", {}).get("cad:boundingRooms", [])) == 2
        ]
        if len(semantic_walls) != 12 or len(shared_semantic_walls) != 4:
            raise RuntimeError(
                "Post-apply topology does not match the normalized physical wall "
                "network"
            )

        add_annotations(adapter)
        if not adapter.save_drawing():
            raise RuntimeError("The completed demo drawing could not be saved")
        adapter.zoom_extents()
        adapter.refresh_view()
        screenshot = adapter.get_screenshot()

        return {
            "success": True,
            "drawing": str(adapter.document.FullName),
            "screenshot": screenshot["path"],
            "preview": {
                "operation_count": preview["operation_count"],
                "physical_wall_count": len(wall_operations),
                "shared_wall_count": len(shared_preview_walls),
                "mutated": preview["mutated"],
            },
            "apply": {
                "status": applied["status"],
                "revision": applied["result"]["revision"],
                "sidecars": applied["result"]["sidecars"],
            },
            "verification": {
                "cache_hit": verified["cache_hit"],
                "semantic_wall_count": len(semantic_walls),
                "shared_semantic_wall_count": len(shared_semantic_walls),
                "by_class": verified["counts"]["by_class"],
            },
            "native_aec": capabilities,
        }
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
