"""scripts/eval_trace.py on a synthetic ground-truth sample (the reference plan).

The sample is built like dataset/gt/training/<id>: image.png, graph.json with
graph_px (nodes with junction types, edges, openings with host edge_nodes and
ends, zones as clear-room polygons) and meta.json with the scale and split.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from tests.test_trace_engine import DOORS, ROOMS, WALLS, WINDOWS, reference_trace
from trace_engine import TraceResult
from trace_engine.graph import clean

ROOT = Path(__file__).resolve().parents[1]


def _load_eval():
    spec = importlib.util.spec_from_file_location("eval_trace", ROOT / "scripts" / "eval_trace.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_sample(root: Path, name: str, building: str, split: str) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from trace_fixtures import render_reference_plan

    mm_per_px, margin = 10.0, 120
    img, _ = render_reference_plan(mm_per_px=mm_per_px, margin_px=margin)
    height_mm = 12192

    def px(p):
        return (margin + p[0] / mm_per_px, margin + (height_mm - p[1]) / mm_per_px)

    net = clean(TraceResult.model_validate(reference_trace()))
    ev = _load_eval()
    nodes, inc = [], {}
    for w in net.walls:
        a, b = net.nodes[w.a], net.nodes[w.b]
        inc.setdefault(w.a, []).append(math.atan2(-(b[1] - a[1]), b[0] - a[0]))
        inc.setdefault(w.b, []).append(math.atan2(-(a[1] - b[1]), a[0] - b[0]))
    for k, p in net.nodes.items():
        q = px(p)
        nodes.append({"id": f"n{k}", "x": q[0], "y": q[1], "degree": len(inc.get(k, [])),
                      "type": ev.junction_type(inc.get(k, []))})
    edges = [{"source": f"n{w.a}", "target": f"n{w.b}", "type": w.type} for w in net.walls]
    openings = []
    for kind, items in (("door", DOORS), ("window", WINDOWS)):
        for wall, off, width in items:
            a, b, _ = WALLS[wall]
            L = math.dist(a, b)
            u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
            e0 = (a[0] + u[0] * off, a[1] + u[1] * off)
            e1 = (a[0] + u[0] * (off + width), a[1] + u[1] * (off + width))
            c = ((e0[0] + e1[0]) / 2, (e0[1] + e1[1]) / 2)
            host = min(net.walls, key=lambda w: ev.dist_to_segs(
                __import__("numpy").array([c]), [(net.nodes[w.a], net.nodes[w.b])])[0])
            openings.append({"kind": kind, "edge_nodes": [f"n{host.a}", f"n{host.b}"],
                             "width_px": width / mm_per_px, "ends_px": [list(px(e0)), list(px(e1))]})
    zones = [{"name": n, "polygon_px": [list(px(p)) for p in ((xl, yb), (xr, yb), (xr, yt), (xl, yt))]}
             for n, (xl, xr, yb, yt) in ROOMS.items()]
    d = root / name
    d.mkdir(parents=True)
    img.save(d / "image.png")
    (d / "graph.json").write_text(json.dumps({"graph_px": {"nodes": nodes, "edges": edges, "openings": openings,
                                                           "zones": zones}}))
    (d / "meta.json").write_text(json.dumps({"group_id": building, "split": split, "annotation_complete": True,
                                             "reviewed": True, "bucket": "manual",
                                             "registration": {"px_per_mm": 1.0 / mm_per_px}}))


@pytest.fixture()
def dataset(tmp_path):
    data = tmp_path / "training"
    _make_sample(data, "planA", "house-1", "val")
    return data


def test_manifest_freeze_and_leakage_check(tmp_path, dataset):
    ev = _load_eval()
    man = tmp_path / "manifest.json"
    assert ev.main(["--data", str(dataset), "--manifest", str(man), "--freeze-manifest"]) == 0
    rows = json.loads(man.read_text())["samples"]
    assert rows[0]["building_id"] == "house-1" and rows[0]["scale"]["px_per_mm"] == pytest.approx(0.1)
    assert ev.leakage(rows + [{**rows[0], "split": "train"}]) == {"house-1": ["train", "val"]}


def test_runner_scores_all_three_stages_on_the_reference_plan(tmp_path, dataset):
    pytest.importorskip("scipy")
    ev = _load_eval()
    man = tmp_path / "manifest.json"
    ev.main(["--data", str(dataset), "--manifest", str(man), "--freeze-manifest"])
    out = tmp_path / "eval"
    ev.main(["--data", str(dataset), "--manifest", str(man), "--no-defaults", "--model", "classic=classic",
             "--out", str(out), "--no-overlays"])
    summary = json.loads((out / "summary.json").read_text())
    assert summary["settings"]["manifest_hash"] and summary["settings"]["wall_tol_mm"] == 150.0
    res = summary["results"]
    assert set(res) == {"classic | raw", "classic | legacy", "classic | canonical"}
    for key, s in res.items():
        assert s["failed"] == 0, key
        assert s["wall_f1"] > 0.9, key
        assert s["window_f1"] > 0.8, key
    for key in ("classic | legacy", "classic | canonical"):
        s = res[key]
        # Every IFC room is found; the classic recogniser also reads the six
        # furniture outlines of the fixture as small rooms (precision 12/18).
        assert s["room_recall"] == 1.0 and s["room_mean_iou"] > 0.9, key
        assert s["room_boundary_err_mm"] < 60, key
        assert s["adjacency_recall"] > 0.8, key
        assert s["invalid_changes"] == 0, key
        assert s["junction_recall"] > 0.9, key  # furniture corners lower precision
    assert (out / "index.html").read_text().count("classic | canonical") >= 1
