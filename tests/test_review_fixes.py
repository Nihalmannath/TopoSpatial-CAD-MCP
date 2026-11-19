"""Regression tests for bugs found in the 30-31 Oct 2026 live review.

Covers: coordinate/point parsing, entity property application, document
acquisition without a drawing, check_running, rollback thread affinity and
manual-undo reconciliation, and geometry unknown-field errors.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adapters import AutoCADAdapter, get_com_worker
from core.exceptions import CADNoDocumentError, InvalidParameterError
from design_engine import DesignOrchestrator
from mcp_tools.helpers import parse_coordinate
from mcp_tools.tools.drawing import _parse_points
from topology_engine.engine import TopologyEngine


# --------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "text, expected",
    [
        ("10.5,-.5", (10.5, -0.5)),
        ("-.25,.75", (-0.25, 0.75)),
        ("1e3,2", (1000.0, 2.0)),
        ("1.5E-2,-3e+1,0", (0.015, -30.0, 0.0)),
        ("+4,-5", (4.0, -5.0)),
    ],
)
def test_parse_coordinate_keeps_signs_and_exponents(text, expected):
    assert parse_coordinate(text) == pytest.approx(expected)


def test_parse_coordinate_rejects_several_points_instead_of_truncating():
    with pytest.raises(InvalidParameterError):
        parse_coordinate("0,0,10,0,10,10")


@pytest.mark.parametrize(
    "value",
    [
        "0,0;100,0;100,100",
        "0,0|100,0|100,100",
        "0,0 100,0 100,100",
        "[0,0],[100,0],[100,100]",
        "[[0,0],[100,0],[100,100]]",
        [[0, 0], [100, 0], [100, 100]],
        ["0,0", "100,0", "100,100"],
    ],
)
def test_parse_points_accepts_common_formats(value):
    points = _parse_points(value)
    assert [tuple(float(c) for c in p) for p in points] == [
        (0.0, 0.0),
        (100.0, 0.0),
        (100.0, 100.0),
    ]


# ----------------------------------------------------- entity properties
class _Entity:
    """Mimics AutoCAD: assigning an unknown layer raises 'Key not found'."""

    def __init__(self, layers):
        self._layers = layers
        self._layer = "0"
        self.Color = 256

    @property
    def Layer(self):
        return self._layer

    @Layer.setter
    def Layer(self, value):
        if value not in self._layers:
            raise Exception("Key not found")
        self._layer = value


def test_apply_properties_creates_missing_layer_and_still_sets_color():
    layers = {"0"}
    adapter = AutoCADAdapter("autocad")
    adapter.document = MagicMock()
    adapter.document.Layers.Add.side_effect = lambda name: layers.add(name)
    entity = _Entity(layers)

    adapter._apply_properties(entity, "T_NEW_LAYER", "red")

    assert entity.Layer == "T_NEW_LAYER"
    assert entity.Color == 1
    adapter.document.Layers.Add.assert_called_once_with("T_NEW_LAYER")


def test_apply_properties_sets_color_even_when_layer_cannot_be_created():
    adapter = AutoCADAdapter("autocad")
    adapter.document = MagicMock()
    adapter.document.Layers.Add.side_effect = Exception("locked")
    entity = _Entity({"0"})

    adapter._apply_properties(entity, "BAD", "yellow")

    assert entity.Layer == "0"
    assert entity.Color == 2


# ------------------------------------------------------ document access
class _FakeApp:
    """AutoCAD application whose ActiveDocument getter is scripted."""

    def __init__(self, active_document, count=0):
        self._active_document = active_document
        self.Documents = MagicMock()
        self.Documents.Count = count

    @property
    def ActiveDocument(self):
        return self._active_document()


def _app_without_documents():
    def no_document():
        raise Exception("No active document")

    return _FakeApp(no_document, count=0)


def test_attach_with_no_drawing_raises_no_document_and_never_adds_one():
    adapter = AutoCADAdapter("autocad")
    adapter.application = _app_without_documents()

    with pytest.raises(CADNoDocumentError):
        adapter._acquire_document(create_if_missing=False)
    adapter.application.Documents.Add.assert_not_called()


def test_explicit_launch_may_create_a_drawing():
    adapter = AutoCADAdapter("autocad")
    adapter.application = _app_without_documents()
    adapter.application.Documents.Add.return_value = "new-doc"

    assert adapter._acquire_document(create_if_missing=True) == "new-doc"


def test_document_access_retries_a_transient_failure():
    adapter = AutoCADAdapter("autocad")
    calls = {"n": 0}
    good_doc = SimpleNamespace(Name="Drawing1.dwg")

    def active_document():
        calls["n"] += 1
        if calls["n"] == 1:
            raise AttributeError("<unknown>.ActiveDocument")
        return good_doc

    app = _FakeApp(active_document, count=1)
    app.Documents.Item.side_effect = Exception("transient")
    adapter.application = app

    with patch("adapters.mixins.connection_mixin.time.sleep"):
        assert adapter._acquire_document() is good_doc


def test_check_running_reports_running_without_document():
    from mcp_tools.tools import session

    with (
        patch.object(session, "get_cad_instances", return_value={}),
        patch.object(
            session,
            "auto_detect_cad",
            side_effect=CADNoDocumentError("autocad"),
        ),
    ):
        result = session._check_running({})

    assert result["any_running"] is True
    assert result["document_open"] is False
    assert result["running_cad_types"] == ["autocad"]


# --------------------------------------------------------------- rollback
def _snapshot(revision):
    return SimpleNamespace(
        revision=revision, drawing_name="Drawing1.dwg", entities=[]
    )


class _ThreadCheckingBridge:
    """Fails like pywin32 does when COM is used off the worker thread."""

    def __init__(self, revision):
        self.revision = revision
        self.rollback_calls = 0

    def _require_worker(self):
        if not get_com_worker().is_current_thread():
            raise RuntimeError("CoInitialize has not been called.")

    def snapshot(self, adapter, scope="all"):
        self._require_worker()
        return _snapshot(self.revision)

    def rollback_last_transaction(self, adapter, *, expected_revision=None, **_):
        self._require_worker()
        self.rollback_calls += 1
        self.revision = expected_revision

    def write_sidecars(self, *args, **kwargs):
        return {}


def _orchestrator_with(bridge, transaction):
    engine = MagicMock()
    engine.analyze.return_value = {"graph": {"@graph": []}}
    engine.to_turtle.return_value = ""
    transactions = MagicMock()
    transactions.get.return_value = transaction
    orchestrator = DesignOrchestrator(
        engine=engine, bridge=bridge, transactions=transactions, cache=MagicMock()
    )
    return orchestrator, transactions


def _applied_transaction():
    return SimpleNamespace(
        transaction_id="tx-1",
        status="applied",
        drawing_name="Drawing1.dwg",
        base_revision="rev-before",
        result={"revision": "rev-after"},
    )


def test_rollback_runs_undo_and_snapshot_on_the_com_worker():
    bridge = _ThreadCheckingBridge("rev-after")
    orchestrator, transactions = _orchestrator_with(bridge, _applied_transaction())
    adapter = MagicMock(cad_type="autocad")

    result = orchestrator._rollback(
        SimpleNamespace(transaction_id="tx-1", detail_level="summary"), adapter, "t"
    )

    assert result["success"] is True
    assert bridge.rollback_calls == 1
    assert bridge.revision == "rev-before"
    transactions.mark_rolled_back.assert_called_once()


def test_rollback_reconciles_a_manual_undo_without_undoing_again():
    bridge = _ThreadCheckingBridge("rev-before")  # user already pressed U
    orchestrator, transactions = _orchestrator_with(bridge, _applied_transaction())

    result = orchestrator._rollback(
        SimpleNamespace(transaction_id="tx-1", detail_level="summary"),
        MagicMock(cad_type="autocad"),
        "t",
    )

    assert result["reconciled"] is True
    assert bridge.rollback_calls == 0
    transactions.mark_rolled_back.assert_called_once()


# ------------------------------------------------------- geometry errors
def test_misspelt_room_field_names_the_field_and_the_contract():
    with pytest.raises(ValueError) as info:
        TopologyEngine.validate_geometry(
            "top:Room", {"origin": [0, 0], "width": 4000, "depth": 3000}
        )
    message = str(info.value)
    assert "width" in message and "depth" in message
    assert "clear_width" in message
    assert "Example" in message


def test_valid_room_geometry_still_passes():
    normalized = TopologyEngine.validate_geometry(
        "top:Room", {"origin": [5000, 0], "clear_width": 4000, "clear_depth": 3000}
    )
    assert normalized["clear_width"] == 4000.0


# ------------------------------------------- handle-based (compensating) rollback
from topology_engine.cad_bridge import XDATA_APP, CADTopologyBridge  # noqa: E402


class _LiveEntity:
    def __init__(self, document, handle):
        self.Handle = handle
        self._document = document
        self.payload = None

    def Delete(self):
        del self._document.entities[self.Handle]

    def SetXData(self, codes, values):
        self.payload = (list(getattr(codes, "value", codes)), list(getattr(values, "value", values)))

    def GetXData(self, _app):
        if self.payload is None:
            raise RuntimeError("no data")
        return self.payload


class _LiveDocument:
    """A drawing whose ModelSpace really grows and shrinks."""

    Name = "Scratch.dwg"
    FullName = ""

    def __init__(self, handles=("A0",)):
        self.entities = {}
        self.next_handle = 0x100
        self.undo_marks = 0
        self.RegisteredApplications = SimpleNamespace(Add=lambda _name: None)
        for handle in handles:
            self.entities[handle] = _LiveEntity(self, handle)

    @property
    def ModelSpace(self):
        items = list(self.entities.values())
        return SimpleNamespace(Count=len(items), Item=lambda index: items[index])

    def add(self):
        handle = format(self.next_handle, "X")
        self.next_handle += 1
        self.entities[handle] = _LiveEntity(self, handle)
        return handle

    def HandleToObject(self, handle):
        return self.entities[handle]

    def GetVariable(self, _name):
        return 4  # millimetres

    def StartUndoMark(self):
        self.undo_marks += 1

    def EndUndoMark(self):
        pass


class _LiveAdapter:
    cad_type = "autocad"

    def __init__(self, document):
        self.document = document
        self.undo_calls = 0

    def _get_document(self, _operation):
        return self.document

    def refresh_view(self):
        return True

    def undo(self, _count):
        self.undo_calls += 1
        return False


def _create_op(name):
    return {"kind": "create_managed", "semantic_id": name, "ontology_class": "top:Wall"}


def test_rollback_deletes_exactly_what_apply_created_without_undo_command():
    document = _LiveDocument()
    adapter = _LiveAdapter(document)
    bridge = CADTopologyBridge()
    before = bridge.snapshot(adapter).revision

    def create(_adapter, doc, operation, host_wall_cache=None):
        listed = doc.add()
        doc.add()  # helper entity (e.g. a door arc) not listed in the result
        return [{"semantic_id": operation["semantic_id"], "handles": [listed]}]

    with patch.object(bridge, "_create_managed", side_effect=create):
        result = bridge.apply_operations(
            adapter, [_create_op("w1"), _create_op("w2")], rollback_revision=before
        )
    assert len(document.entities) == 5
    assert len(result["journal"]["created_handles"]) == 4

    with patch.object(bridge, "rollback_last_transaction") as undo_command:
        info = bridge.rollback_transaction(adapter, result, expected_revision=before)

    undo_command.assert_not_called()
    assert info == {"method": "compensate", "deleted": 4, "xdata_restored": 0}
    assert set(document.entities) == {"A0"}
    assert bridge.snapshot(adapter).revision == before


def test_failed_apply_removes_partial_entities_without_undo_command():
    document = _LiveDocument()
    adapter = _LiveAdapter(document)
    bridge = CADTopologyBridge()
    before = bridge.snapshot(adapter).revision
    calls = {"n": 0}

    def create(_adapter, doc, operation, host_wall_cache=None):
        calls["n"] += 1
        handle = doc.add()
        if calls["n"] == 2:
            raise RuntimeError("wall style not found")  # fails after creating
        return [{"semantic_id": operation["semantic_id"], "handles": [handle]}]

    with (
        patch.object(bridge, "_create_managed", side_effect=create),
        patch.object(bridge, "rollback_last_transaction") as undo_command,
        pytest.raises(RuntimeError, match="wall style not found"),
    ):
        bridge.apply_operations(
            adapter, [_create_op("w1"), _create_op("w2")], rollback_revision=before
        )

    undo_command.assert_not_called()
    assert set(document.entities) == {"A0"}
    assert bridge.snapshot(adapter).revision == before


def test_rollback_restores_xdata_on_annotated_existing_entities():
    document = _LiveDocument()
    adapter = _LiveAdapter(document)
    bridge = CADTopologyBridge()
    before = bridge.snapshot(adapter).revision

    result = bridge.apply_operations(
        adapter,
        [
            {
                "kind": "annotate_handles",
                "handles": ["A0"],
                "semantic_id": "urn:wall:1",
                "ontology_class": "top:Wall",
                "label": "Wall 1",
            }
        ],
    )
    assert bridge.read_xdata(document.entities["A0"])["semantic_id"] == "urn:wall:1"

    info = bridge.rollback_transaction(adapter, result, expected_revision=before)

    assert info["method"] == "compensate" and info["xdata_restored"] == 1
    assert bridge.read_xdata(document.entities["A0"]) == {}
    assert document.entities["A0"].payload == ([1001], [XDATA_APP])
    assert bridge.snapshot(adapter).revision == before


def test_deleting_batches_still_use_the_undo_command():
    document = _LiveDocument(handles=("A0", "A1"))
    adapter = _LiveAdapter(document)
    bridge = CADTopologyBridge()

    result = bridge.apply_operations(
        adapter, [{"kind": "delete_managed", "handles": ["A1"]}]
    )
    assert result["journal"] is None

    with patch.object(bridge, "rollback_last_transaction") as undo_command:
        info = bridge.rollback_transaction(adapter, result, expected_revision=None)

    undo_command.assert_called_once()
    assert info == {"method": "undo_command"}


def test_orchestrator_rollback_uses_the_journal_on_the_com_worker():
    document = _LiveDocument()
    adapter = _LiveAdapter(document)
    bridge = CADTopologyBridge()
    before = bridge.snapshot(adapter).revision
    created = document.add()
    after = bridge.snapshot(adapter).revision
    cad_result = {"journal": {"created_handles": [created], "xdata_before": {}}}
    transaction = SimpleNamespace(
        transaction_id="tx-2",
        status="applied",
        drawing_name="Scratch.dwg",
        base_revision=before,
        result={"revision": after, "cad": cad_result},
    )
    worker_threads = []
    original = bridge.rollback_transaction

    def on_worker(*args, **kwargs):
        worker_threads.append(get_com_worker().is_current_thread())
        return original(*args, **kwargs)

    orchestrator, transactions = _orchestrator_with(bridge, transaction)
    with (
        patch.object(bridge, "rollback_transaction", side_effect=on_worker),
        patch.object(bridge, "write_sidecars", return_value={}),
    ):
        result = orchestrator._rollback(
            SimpleNamespace(transaction_id="tx-2", detail_level="summary"), adapter, "t"
        )

    assert worker_threads == [True]
    assert result["success"] is True
    assert set(document.entities) == {"A0"}
    assert transactions.mark_rolled_back.call_args[0][1]["method"] == "compensate"


def test_room_with_explicit_boundary_can_be_applied():
    """Preview accepted boundary rooms but apply raised KeyError: 'origin'."""
    document = _LiveDocument(handles=())
    adapter = _LiveAdapter(document)
    adapter.draw_polyline = MagicMock(side_effect=lambda *a, **k: document.add())
    bridge = CADTopologyBridge()
    boundary = [[230, 230], [6125, 230], [6125, 5225], [230, 5225]]

    with patch.object(bridge, "_ensure_layer"):
        result = bridge.apply_operations(
            adapter,
            [
                {
                    "kind": "create_managed",
                    "semantic_id": "urn:topospatial:room:living",
                    "ontology_class": "top:Room",
                    "label": "Living",
                    "geometry": {"boundary": boundary},
                }
            ],
        )

    assert result["success"] is True
    points = adapter.draw_polyline.call_args[0][0]
    assert [(p[0], p[1]) for p in points] == [tuple(p) for p in boundary]
    semantic = bridge.read_xdata(next(iter(document.entities.values())))
    assert semantic["ontology_class"] == "top:Room"
    assert semantic["geometry"] == {"boundary": [[float(x), float(y)] for x, y in boundary]}
