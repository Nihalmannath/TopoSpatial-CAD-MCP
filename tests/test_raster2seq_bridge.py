"""Unit tests for Raster2Seq to CAD TraceResult bridge."""

import pytest
from trace_engine.compiler import compile_trace
from trace_engine.models import TraceResult
from trace_engine.raster2seq_bridge import assess_trace_quality, raster2seq_to_trace, unproject_point


def test_unproject_point():
    # Square aspect ratio 256x256 -> 1000x1000
    pt = unproject_point(128.0, 128.0, 1000, 1000, target_size=256)
    assert abs(pt[0] - 500.0) < 5.0
    assert abs(pt[1] - 500.0) < 5.0


def test_raster2seq_to_trace_synthetic():
    # 2 adjacent rooms and 1 door opening
    predictions = [
        {
            "category_id": 2,  # Living Room
            "segmentation": [
                [50.0, 50.0],
                [50.0, 150.0],
                [100.0, 150.0],
                [100.0, 50.0],
                [50.0, 50.0],
            ],
        },
        {
            "category_id": 1,  # Kitchen
            "segmentation": [
                [100.0, 50.0],
                [100.0, 150.0],
                [180.0, 150.0],
                [180.0, 50.0],
                [100.0, 50.0],
            ],
        },
        {
            "category_id": 10,  # Door on the shared wall at x=100
            "segmentation": [
                [100.0, 80.0],
                [100.0, 120.0],
            ],
        },
    ]

    trace = raster2seq_to_trace(
        predictions,
        orig_size=(1000, 1000),
        mm_per_px=10.0,
        wall_thickness_mm=150.0,
    )

    assert isinstance(trace, TraceResult)
    assert len(trace.walls_graph.nodes) >= 4
    assert len(trace.walls_graph.edges) >= 4
    assert len(trace.zones_list) == 2
    assert trace.zones_list[0].name == "Living Room"
    assert trace.zones_list[1].name == "Kitchen"

    # Verify compilability
    compiled = compile_trace(trace)
    summary = compiled.summary()
    assert summary["counts"].get("wall", 0) > 0
    assert summary["counts"].get("room", 0) == 2


def test_crop_origin_is_restored_and_image_axis_is_declared():
    predictions = [{
        "category_id": 2,
        "segmentation": [[32.0, 32.0], [32.0, 224.0], [224.0, 224.0], [224.0, 32.0]],
    }]
    trace = raster2seq_to_trace(
        predictions,
        orig_size=(1000, 1000),
        mm_per_px=2.0,
        origin_px=(100.0, 200.0),
    )
    assert trace.y_down is True
    assert min(node.x for node in trace.walls_graph.nodes) >= 100.0 * 2.0
    assert min(node.y for node in trace.walls_graph.nodes) >= 200.0 * 2.0


def test_quality_gate_rejects_collapsed_prediction():
    collapsed = raster2seq_to_trace(
        [{
            "category_id": 8,
            "segmentation": [[40.0, 40.0], [41.0, 40.0], [41.0, 41.0]],
        }],
        orig_size=(256, 256),
        mm_per_px=10.0,
    )
    quality = assess_trace_quality(collapsed)
    assert quality.collapsed is True
    assert quality.score < 0.1
