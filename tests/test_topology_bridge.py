"""Tests for XData encoding and safe CAD bridge helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from topology_engine import DrawingSnapshot
from topology_engine.cad_bridge import XDATA_APP, CADTopologyBridge


class FakeRegisteredApplications:
    def __init__(self) -> None:
        self.names = set()

    def Add(self, name):
        self.names.add(name)


class FakeEntity:
    def __init__(self, handle="A1") -> None:
        self.Handle = handle
        self.payload = None

    def SetXData(self, codes, values) -> None:
        code_values = getattr(codes, "value", codes)
        data_values = getattr(values, "value", values)
        self.payload = (list(code_values), list(data_values))

    def GetXData(self, _app):
        if self.payload is None:
            raise RuntimeError("no data")
        return self.payload


class FakeDocument:
    def __init__(self, entity) -> None:
        self.RegisteredApplications = FakeRegisteredApplications()
        self.entity = entity
        self.undo_started = False
        self.undo_ended = False

    def HandleToObject(self, _handle):
        return self.entity

    def StartUndoMark(self):
        self.undo_started = True

    def EndUndoMark(self):
        self.undo_ended = True


class FakeAdapter:
    def __init__(self, document) -> None:
        self.document = document
        self.refreshed = False
        self.undone = False

    def _get_document(self, _operation):
        return self.document

    def refresh_view(self):
        self.refreshed = True

    def undo(self, _count):
        self.undone = True


def test_xdata_roundtrip_chunks_large_semantic_payload() -> None:
    bridge = CADTopologyBridge()
    entity = FakeEntity()
    document = FakeDocument(entity)
    semantic = {
        "semantic_id": "urn:uuid:test",
        "ontology_class": "top:Room",
        "label": "Room " + "x" * 600,
        "managed": True,
    }

    bridge.write_xdata(document, entity, semantic)

    assert XDATA_APP in document.RegisteredApplications.names
    assert bridge.read_xdata(entity)["semantic_id"] == "urn:uuid:test"
    assert bridge.read_xdata(entity)["label"] == semantic["label"]
    assert len(entity.payload[1]) > 3


def test_apply_annotation_uses_one_undo_group() -> None:
    bridge = CADTopologyBridge()
    entity = FakeEntity()
    document = FakeDocument(entity)
    adapter = FakeAdapter(document)

    result = bridge.apply_operations(
        adapter,
        [
            {
                "kind": "annotate_handles",
                "handles": ["A1"],
                "semantic_id": "urn:wall:1",
                "ontology_class": "top:Wall",
                "label": "Wall 1",
            }
        ],
    )

    assert result["success"] is True
    assert document.undo_started and document.undo_ended
    assert adapter.refreshed is True
    assert bridge.read_xdata(entity)["managed"] is False


def test_wall_polygon_respects_centerline_and_thickness() -> None:
    polygon = CADTopologyBridge._wall_polygon([0, 0], [5000, 0], 200)

    assert polygon == [(0.0, 100.0), (5000.0, 100.0), (5000.0, -100.0), (0.0, -100.0)]


def test_room_rotation_transform() -> None:
    transformed = CADTopologyBridge._transform((5000, 0), (100, 200), 90)

    assert transformed[0] == pytest.approx(100)
    assert transformed[1] == pytest.approx(5200)


def test_room_walls_produce_requested_clear_and_exterior_dimensions() -> None:
    specs = CADTopologyBridge._room_wall_specs(5000, 4000, 200)
    polygons = [
        CADTopologyBridge._wall_polygon(start, end, 200) for start, end in specs
    ]
    xs = [point[0] for polygon in polygons for point in polygon]
    ys = [point[1] for polygon in polygons for point in polygon]

    assert min(xs) == pytest.approx(-200)
    assert max(xs) == pytest.approx(5200)
    assert min(ys) == pytest.approx(-200)
    assert max(ys) == pytest.approx(4200)
    assert max(xs) - min(xs) == pytest.approx(5400)
    assert max(ys) - min(ys) == pytest.approx(4400)


@pytest.mark.parametrize(
    ("closed", "opened"),
    [((1, 0), (0, 1)), ((1, 0), (0, -1)), ((-1, 0), (0, 1)), ((-1, 0), (0, -1))],
)
def test_door_swing_arc_is_always_a_quarter_circle(closed, opened) -> None:
    start, end = CADTopologyBridge._door_arc_angles(closed, opened)

    assert (end - start) % 360 == pytest.approx(90)


def test_failed_apply_rolls_back_the_undo_group() -> None:
    bridge = CADTopologyBridge()
    document = FakeDocument(FakeEntity())
    adapter = FakeAdapter(document)

    with pytest.raises(ValueError, match="Unknown topology operation"):
        bridge.apply_operations(adapter, [{"kind": "unsupported"}])

    assert document.undo_started and document.undo_ended
    assert adapter.undone is True


def test_sidecars_are_written_atomically(monkeypatch, tmp_path) -> None:
    bridge = CADTopologyBridge()
    snapshot = DrawingSnapshot("Floor Plan.dwg", "", "mm", [])
    graph = {"@context": {}, "@graph": []}
    monkeypatch.setattr(
        "topology_engine.cad_bridge.get_config",
        lambda: SimpleNamespace(output=SimpleNamespace(directory=str(tmp_path))),
    )

    paths = bridge.write_sidecars(snapshot, graph, "# topology\n")

    assert json.loads((tmp_path / "Floor Plan.topology.jsonld").read_text()) == graph
    assert (tmp_path / "Floor Plan.topology.ttl").read_text() == "# topology\n"
    assert paths == {
        "jsonld": str(tmp_path / "Floor Plan.topology.jsonld"),
        "ttl": str(tmp_path / "Floor Plan.topology.ttl"),
    }
    assert not list(tmp_path.glob("*.tmp"))
