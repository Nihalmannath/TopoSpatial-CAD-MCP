"""Canonical geometry pipeline: contract, frame, lineage, rooms, exposure, compile.

Covers the regression cases from the tracing plan (steps 2-6): non-unit scale,
translated origin, Y inversion, reversed edges, perception -> compilation round
trip, merged / split walls with openings, parallel walls, duplicate edges,
overlapping openings, conflicting wall specs, gap repair limits, exposure,
label seeds and room identity.
"""

from __future__ import annotations

import copy
import math

import pytest

from tests.test_trace_engine import DOORS, ROOMS, WALLS, WINDOWS, X0, Y0, _opening_center, reference_trace
from trace_engine import TraceResult, compile_trace
from trace_engine.canonical import Frame, normalize, to_trace_result
from trace_engine.geometry_kernel import (
    DEFAULT_WALL_THICKNESS,
    CoordinateProjector,
    compute_face_content_hash,
    solve_wall_exposure,
)
from trace_engine.models import SCHEMA_VERSION, upgrade_trace
from trace_engine.quality import assess
from trace_engine.regularize import (
    RegularizeConfig,
    canonical_face_hash,
    regularize_trace,
    representative_point,
)
from trace_engine.rooms import point_in_polygon


def _plan(nodes, edges, doors=(), windows=(), zones=(), **extra):
    data = {
        "walls_graph": {"nodes": [{"id": k, "x": x, "y": y} for k, (x, y) in nodes.items()],
                        "edges": [dict(e) for e in edges]},
        "doors_list": list(doors), "windows_list": list(windows), "zones_list": list(zones), "units": "mm",
    }
    data.update(extra)
    return data


def _box(x0, y0, x1, y1, t=200, prefix="b", kind="external", source="measured"):
    nodes = {f"{prefix}0": (x0, y0), f"{prefix}1": (x1, y0), f"{prefix}2": (x1, y1), f"{prefix}3": (x0, y1)}
    edges = [{"id": f"{prefix}e{i}", "source": f"{prefix}{i}", "target": f"{prefix}{(i + 1) % 4}", "type": kind,
              "thickness": t, "thickness_source": source} for i in range(4)]
    return nodes, edges


def _codes(reg):
    return [d.code for d in reg.diagnostics]


# ------------------------------------------------------------------ contract
def test_v1_payload_still_validates_and_upgrade_adds_stable_ids_without_mutation():
    data = reference_trace()
    before = copy.deepcopy(data)
    v1 = TraceResult.model_validate(data)
    assert v1.schema_version == 1 and v1.walls_graph.edges[0].id is None
    v2 = upgrade_trace(data)
    assert data == before
    assert v2.schema_version == SCHEMA_VERSION
    assert [e.id for e in v2.walls_graph.edges][:3] == ["e0", "e1", "e2"]
    assert v2.doors_list[0].id == "d0" and v2.windows_list[0].id == "w0"
    assert upgrade_trace(v2).walls_graph.edges[5].id == "e5"  # idempotent


def test_duplicate_edge_ids_are_rejected():
    nodes, edges = _box(0, 0, 4000, 3000)
    edges[1]["id"] = edges[0]["id"]
    with pytest.raises(ValueError, match="edge ids must be unique"):
        TraceResult.model_validate(_plan(nodes, edges))


def test_opening_centre_and_start_offsets_are_distinct():
    op = TraceResult.model_validate(reference_trace()).windows_list[0]
    assert op.center_offset == op.distance_from_lower_node
    assert op.start_offset == pytest.approx(op.center_offset - op.length / 2)


# ------------------------------------------------------------------ frame
def _as_pixels(data, mm_per_px=20.0, origin=(300.0, 50.0)):
    """Express a mm y-up plan as y-down pixels with a translated origin."""
    px = copy.deepcopy(data)
    ox, oy = origin

    def to_px(p):
        return [p[0] / mm_per_px + ox, -p[1] / mm_per_px + oy]

    for n in px["walls_graph"]["nodes"]:
        n["x"], n["y"] = to_px((n["x"], n["y"]))
    for e in px["walls_graph"]["edges"]:
        e["thickness"] /= mm_per_px
    for o in px["doors_list"] + px["windows_list"]:
        o["length"] /= mm_per_px
        if o.get("center") is not None:
            o["center"] = to_px(o["center"])
        if o.get("distance_from_lower_node") is not None:
            o["distance_from_lower_node"] /= mm_per_px
    for z in px["zones_list"]:
        z["seedPoint"] = to_px(z["seedPoint"])
    px.update(units="px", mm_per_unit=mm_per_px, y_down=True,
              frame={"origin": [ox, oy], "calibrated": True, "scale_source": "test"})
    return px


def test_frame_round_trip_with_scale_translation_and_y_inversion():
    f = Frame(mm_per_unit=12.5, y_down=True, origin=(40.0, 900.0))
    p = (123.0, 456.0)
    assert f.from_mm(f.to_mm(p)) == pytest.approx(p)
    assert f.to_mm((40.0, 899.0)) == pytest.approx((0.0, 12.5))  # one px up = +y in mm
    with pytest.raises(ValueError):
        Frame(mm_per_unit=0)


def test_pixels_and_millimetres_give_the_same_compiled_geometry():
    mm = reference_trace(noise_mm=0.0, split_one_wall=True)
    a = compile_trace(TraceResult.model_validate(mm), geometry_pipeline="canonical")
    b = compile_trace(TraceResult.model_validate(_as_pixels(mm)), geometry_pipeline="canonical")

    def key(c):
        g = c["geometry"]
        if c["@type"] == "top:Wall":
            return ("w", tuple(sorted((tuple(g["start"]), tuple(g["end"])))), g["thickness"])
        if c["@type"] == "top:Room":
            return ("r", c["label"], tuple(sorted(tuple(p) for p in g["boundary"])))
        return ("o", c["@type"], c["label"], g["offset"], g["width"])

    ka = sorted(map(key, a.changes))
    kb = sorted(map(key, b.changes))
    assert len(ka) == len(kb)
    for x, y in zip(ka, kb):
        assert x[0] == y[0]
        if x[0] == "w":
            for p, q in zip(x[1], y[1]):
                assert p == pytest.approx(q, abs=1.0)
            assert x[2] == pytest.approx(y[2], abs=1)
        elif x[0] == "r":
            assert x[1] == y[1]
            for p, q in zip(x[2], y[2]):
                assert p == pytest.approx(q, abs=1.0)
        else:
            assert x[1:3] == y[1:3]
            assert x[3] == pytest.approx(y[3], abs=1) and x[4] == pytest.approx(y[4], abs=1)


def test_normalize_does_not_modify_input_and_converts_lengths():
    px = _as_pixels(reference_trace())
    before = copy.deepcopy(px)
    plan = normalize(px)
    assert px == before
    assert plan.edges[0].thickness == pytest.approx(230)
    assert plan.openings[0].width == pytest.approx(DOORS[0][2])


# ------------------------------------------------------------------ kernel fixes
def test_projector_y_flip_and_anisotropy_guard():
    pr = CoordinateProjector.from_scale(20.0, source_height=100.0, y_down=True)
    assert pr.map_point({"x": 1.0, "y": 0.0}) == {"x": 20.0, "y": 2000.0}
    assert pr.map_point({"x": 1.0, "y": 100.0})["y"] == pytest.approx(0.0)
    assert pr.map_vector(0, 1) == (0, -20.0)
    with pytest.raises(ValueError, match="anisotropic"):
        CoordinateProjector({"x": 0, "y": 0, "width": 1, "height": 1}, {"x": 0, "y": 0, "width": 2, "height": 3})
    CoordinateProjector({"x": 0, "y": 0, "width": 1, "height": 1}, {"x": 0, "y": 0, "width": 2, "height": 3},
                        allow_anisotropic=True)


def test_kernel_exposure_probes_keep_measured_thickness_and_report_unknown_and_mixed():
    nodes = {"a": {"x": 0, "y": 0}, "b": {"x": 4000, "y": 0}, "c": {"x": 0, "y": 9000}, "d": {"x": 4000, "y": 9000}}
    room = [(0, 0), (4000, 0), (4000, 3000), (0, 3000)]
    walls = [
        {"id": "measured", "startNodeId": "a", "endNodeId": "b", "type": "internal", "thickness": 230.0},
        {"id": "nominal", "startNodeId": "a", "endNodeId": "b", "type": "internal", "thickness": 150.0,
         "thicknessSource": "default"},
        {"id": "far", "startNodeId": "c", "endNodeId": "d", "type": "internal", "thickness": 200.0},
    ]
    out = {w["id"]: w for w in solve_wall_exposure(walls, nodes, [room])}
    assert out["measured"]["exposure"] == "external" and out["measured"]["thickness"] == 230.0
    assert out["nominal"]["thickness"] == DEFAULT_WALL_THICKNESS["externalMm"]
    assert out["far"]["exposure"] == "unknown" and out["far"]["type"] == "internal"
    half = [(0, 0), (2000, 0), (2000, 3000), (0, 3000)]
    mixed = solve_wall_exposure(walls[:1], nodes, [half], probe_count=8)[0]
    assert mixed["exposure"] == "mixed" and mixed["exposureTransitions"] == [pytest.approx(0.5, abs=0.07)]




@pytest.mark.parametrize("y_down", [False, True])
def test_kernel_tracer_uses_one_frame_and_keeps_door_attributes(y_down):
    pytest.importorskip("shapely")
    from trace_engine.geometry_kernel import PlanGeometryTracer

    data = reference_trace()
    data["doors_list"][0].update(hinge="end", swing="right", door_type="double")
    if y_down:
        data = _as_pixels(data, mm_per_px=10.0, origin=(0.0, 0.0))
        data["walls_graph"]["nodes"] = [{**n, "y": n["y"] + 1300} for n in data["walls_graph"]["nodes"]]
        for o in data["doors_list"]:
            o["center"][1] += 1300
        for z in data["zones_list"]:
            z["seedPoint"][1] += 1300
    before = copy.deepcopy(data)
    tracer = PlanGeometryTracer()
    out = tracer.trace(data, image_size=(1600, 1300) if y_down else (15240, 12192),
                       mm_per_unit=10.0 if y_down else 1.0, y_down=y_down)
    assert data == before  # input untouched
    assert out["metrics"]["roomCount"] == 12
    assert out["metrics"]["doorCount"] == 13 and out["metrics"]["windowCount"] == 13
    ext = [w for w in out["walls"] if w["exposure"] == "external"]
    assert ext and all(w["type"] == "external" for w in ext)
    tr = tracer.to_trace_result(out)
    assert any(d.door_type == "double" for d in tr.doors_list)
    compiled = compile_trace(tr)
    assert compiled.summary()["counts"]["room"] == 12


def test_canonical_face_hash_ignores_winding_start_and_closing_vertex():
    sq = [(0.0, 0.0), (3000.0, 0.0), (3000.0, 4000.0), (0.0, 4000.0)]
    rotated = sq[2:] + sq[:2]
    reversed_ = list(reversed(sq)) + [sq[-1]]
    jitter = [(x + 0.3, y - 0.2) for x, y in sq]
    collinear = [sq[0], (1500.0, 0.0)] + sq[1:]
    h = canonical_face_hash(sq)
    assert h == canonical_face_hash(rotated) == canonical_face_hash(reversed_) == canonical_face_hash(jitter)
    assert h == canonical_face_hash(collinear)
    assert compute_face_content_hash(sq) != compute_face_content_hash(rotated)  # the raw kernel hash is order-dependent
    assert h != canonical_face_hash([(0, 0), (3000, 0), (3000, 4100), (0, 4100)])


# ------------------------------------------------------------------ lineage / openings
def test_every_input_edge_has_lineage_and_every_opening_is_hosted_or_explained():
    reg = regularize_trace(reference_trace(noise_mm=20, split_one_wall=True))
    assert set(reg.edge_lineage) == {e.id for e in reg.plan.edges}
    assert all(reg.edge_lineage.values())
    for h in reg.openings:
        assert h.hosted or any(d.entity == h.id for d in reg.diagnostics)
    assert sum(h.hosted for h in reg.openings) == 26
    # int-h2 doors past the split point (x=8000) are reported against the first
    # piece. The one on the merged 5800-9700 run is still in that piece's
    # lineage; only the one beyond 9700 needs the bounded geometric fallback.
    fallback = [h for h in reg.openings if h.method == "fallback"]
    assert [h.id for h in fallback] == ["d8"] and fallback[0].position[0] > 9700


def test_opening_on_reversed_merged_segment_keeps_position_and_flips_hinge():
    # One straight wall reported as two pieces, the second one reversed.
    nodes = {"a": (0, 0), "m": (3000, 0), "b": (6000, 0), "c": (6000, 3000), "d": (0, 3000)}
    edges = [
        {"id": "p1", "source": "a", "target": "m", "type": "external", "thickness": 200},
        {"id": "p2", "source": "b", "target": "m", "type": "external", "thickness": 200},  # reversed
        {"id": "e", "source": "b", "target": "c", "type": "external", "thickness": 200},
        {"id": "n", "source": "c", "target": "d", "type": "external", "thickness": 200},
        {"id": "w", "source": "d", "target": "a", "type": "external", "thickness": 200},
    ]
    door = {"id": "D", "edge_nodes": ["b", "m"], "length": 900, "distance_from_lower_node": 1500,
            "hinge": "start", "swing": "left"}
    reg = regularize_trace(_plan(nodes, edges, doors=[door]))
    h = reg.openings[0]
    assert h.method == "lineage" and h.hosted
    wall = reg.network.walls[h.wall_index]
    a, b = reg.network.nodes[wall.a], reg.network.nodes[wall.b]
    assert {round(a[0]), round(b[0])} == {0, 6000}
    centre_x = a[0] + (b[0] - a[0]) / math.dist(a, b) * h.center_offset
    assert centre_x == pytest.approx(4500)
    assert h.reversed == (a[0] < b[0])
    assert (h.hinge, h.swing) == (("end", "right") if h.reversed else ("start", "left"))
    assert sorted(reg.wall_ids)[0]  # deterministic ids exist
    assert any("+" in i for i in reg.wall_ids)  # merged wall named from both sources


def test_t_junction_split_hosts_opening_on_the_correct_piece():
    nodes, edges = _box(0, 0, 8000, 4000)
    nodes.update({"t": (4000, 0), "u": (4000, 4000)})
    edges.append({"id": "part", "source": "t", "target": "u", "type": "internal", "thickness": 120})
    window = {"id": "W", "edge_nodes": ["b0", "b1"], "length": 1200, "distance_from_lower_node": 6000}
    reg = regularize_trace(_plan(nodes, edges, windows=[window]))
    assert len(reg.edge_lineage["be0"]) == 2  # split at the T
    h = reg.openings[0]
    wall = reg.network.walls[h.wall_index]
    xs = sorted(reg.network.nodes[n][0] for n in (wall.a, wall.b))
    assert xs == pytest.approx([4000, 8000])
    assert h.method == "lineage"
    assert reg.wall_ids[h.wall_index].startswith("be0.")


def test_nearby_parallel_wall_does_not_steal_the_opening():
    # Two parallel walls 120 mm apart (e.g. a duct). The door is reported on
    # the upper one but drawn slightly nearer the lower one.
    nodes = {"a": (0, 0), "b": (5000, 0), "c": (0, 120), "d": (5000, 120)}
    edges = [{"id": "low", "source": "a", "target": "b", "thickness": 100},
             {"id": "high", "source": "c", "target": "d", "thickness": 100}]
    door = {"id": "D", "edge_nodes": ["c", "d"], "length": 900, "center": [2500, 50]}
    reg = regularize_trace(_plan(nodes, edges, doors=[door]), RegularizeConfig(repair_gaps=False))
    h = reg.openings[0]
    assert h.hosted and reg.wall_ids[h.wall_index] == "high" and h.method == "lineage"


def test_duplicate_edges_with_conflicting_explicit_thickness_are_reported():
    nodes, edges = _box(0, 0, 4000, 3000)
    edges.append({"id": "dup", "source": "b1", "target": "b0", "type": "external", "thickness": 400})
    reg = regularize_trace(_plan(nodes, edges))
    assert "duplicate_wall_thickness_conflict" in _codes(reg)
    assert len([w for w in reg.network.walls]) == 4
    merged = next(w for w in reg.network.walls if "dup" in w.sources)
    assert merged.sources == {"be0", "dup"} and merged.thickness == 400


def test_conflicting_wall_specs_on_one_straight_run_are_kept_apart():
    nodes = {"a": (0, 0), "m": (3000, 0), "b": (6000, 0)}
    edges = [{"id": "x", "source": "a", "target": "m", "thickness": 300},
             {"id": "y", "source": "m", "target": "b", "thickness": 100}]
    reg = regularize_trace(_plan(nodes, edges), RegularizeConfig(repair_gaps=False))
    assert len(reg.network.walls) == 2
    assert "colinear_kept_apart" in _codes(reg)


def test_overlapping_openings_are_rejected_explicitly():
    nodes, edges = _box(0, 0, 6000, 4000)
    a = {"id": "D1", "edge_nodes": ["b0", "b1"], "length": 1000, "distance_from_lower_node": 2000}
    b = {"id": "D2", "edge_nodes": ["b0", "b1"], "length": 1000, "distance_from_lower_node": 2400}
    reg = regularize_trace(_plan(nodes, edges, doors=[a, b]))
    assert reg.openings[0].hosted and not reg.openings[1].hosted
    assert any(d.code == "opening_overlap" and d.entity == "D2" for d in reg.diagnostics)


def test_opening_wider_than_wall_and_unknown_host_are_diagnosed():
    nodes, edges = _box(0, 0, 3000, 3000)
    wide = {"id": "W1", "edge_nodes": ["b0", "b1"], "length": 3500, "distance_from_lower_node": 1500}
    lost = {"id": "W2", "edge_nodes": ["zz", "yy"], "length": 900, "center": [1500, 9000]}
    reg = regularize_trace(_plan(nodes, edges, windows=[wide, lost]))
    codes = {(d.code, d.entity) for d in reg.diagnostics}
    assert ("opening_wider_than_wall", "W1") in codes and ("opening_no_host", "W2") in codes


# ------------------------------------------------------------------ rooms
def _gapped_room(gap):
    nodes = {"a": (0, 0), "b": (5000, 0), "c": (5000, 4000), "d": (0, 4000), "e": (0, gap)}
    edges = [{"id": "s", "source": "a", "target": "b", "thickness": 200, "type": "external"},
             {"id": "e", "source": "b", "target": "c", "thickness": 200, "type": "external"},
             {"id": "n", "source": "c", "target": "d", "thickness": 200, "type": "external"},
             {"id": "w", "source": "d", "target": "e", "thickness": 200, "type": "external"}]
    return _plan(nodes, edges, zones=[{"name": "Hall", "seedPoint": [2500, 2000]}])


def test_small_gap_is_repaired_but_a_doorway_sized_gap_is_not():
    small = regularize_trace(_gapped_room(250))
    assert [r.label for r in small.rooms] == ["Hall"]
    assert "gap_extended" in _codes(small)
    assert any(w.provenance == "gap_repair" or "w" in w.sources for w in small.network.walls)
    door_gap = regularize_trace(_gapped_room(900))
    assert door_gap.rooms == []
    assert "dangling_wall_ends" in _codes(door_gap)
    off = regularize_trace(_gapped_room(250), RegularizeConfig(repair_gaps=False))
    assert off.rooms == []


def test_reference_plan_rooms_exposure_and_thickness():
    data = reference_trace(noise_mm=20, split_one_wall=True)
    reg = regularize_trace(data)
    assert sorted(r.label for r in reg.rooms) == sorted(ROOMS)
    by_src = {}
    for w, e in zip(reg.network.walls, reg.exposure):
        for s in w.sources:
            by_src.setdefault(s, set()).add(e)
    names = list(WALLS)
    for i, name in enumerate(names):
        kinds = set().union(*(by_src.get(eid, set()) for eid in reg.edge_lineage if eid == f"e{i}" or eid.startswith(f"e{i}.")))
        if name.startswith("ext"):
            assert kinds == {"external"}, name
    assert all(w.thickness in (230, 150) for w in reg.network.walls)  # measured thickness kept


def test_default_thickness_is_resolved_by_exposure():
    nodes, edges = _box(0, 0, 8000, 5000, t=100, source="default", kind="internal")
    nodes.update({"t": (4000, 0), "u": (4000, 5000)})
    edges.append({"id": "part", "source": "t", "target": "u", "type": "internal", "thickness": 100,
                  "thickness_source": "default"})
    reg = regularize_trace(_plan(nodes, edges))
    for w, e in zip(reg.network.walls, reg.exposure):
        if "part" in w.sources:
            assert e == "internal" and w.thickness == DEFAULT_WALL_THICKNESS["internalMm"]
        else:
            assert e == "external" and w.type == "external" and w.thickness == DEFAULT_WALL_THICKNESS["externalMm"]


def test_courtyard_labelled_outdoor_makes_its_walls_external():
    nodes, edges = _box(0, 0, 9000, 9000, prefix="o")
    inner, inner_edges = _box(3000, 3000, 6000, 6000, prefix="i", kind="internal")
    nodes.update(inner)
    # Tie the courtyard to the outer wall so the plan is one component.
    nodes.update({"t": (4500, 0)})
    inner_edges.append({"id": "link", "source": "t", "target": "i0", "type": "internal", "thickness": 150})
    zones = [{"name": "Courtyard", "seedPoint": [4500, 4500]}, {"name": "Hall", "seedPoint": [1500, 7500]}]
    reg = regularize_trace(_plan(nodes, edges + inner_edges, zones=zones))
    court = next(r for r in reg.rooms if r.label == "Courtyard")
    assert court.outdoor
    for w, e in zip(reg.network.walls, reg.exposure):
        if any(s.startswith("ie") for s in w.sources):
            assert e == "external"


def test_label_seeds_boundary_conflicts_and_unclassified_rooms():
    nodes, edges = _box(0, 0, 8000, 4000)
    nodes.update({"t": (4000, 0), "u": (4000, 4000)})
    edges.append({"id": "part", "source": "t", "target": "u", "type": "internal", "thickness": 120})
    zones = [{"name": "Kitchen", "seedPoint": [2000, 2000]}, {"name": "Dining", "seedPoint": [1000, 1000]},
             {"name": "Edge", "seedPoint": [3950, 2500]}]
    reg = regularize_trace(_plan(nodes, edges, zones=zones))
    codes = _codes(reg)
    assert "label_conflict" in codes and "label_on_boundary" in codes
    left = next(r for r in reg.rooms if "Kitchen" in r.names)
    assert set(left.names) == {"Kitchen", "Dining", "Edge"}
    right = next(r for r in reg.rooms if r is not left)
    assert not right.classified and right.names == []
    assert "room_unclassified" in codes
    compiled = compile_trace(reg)
    assert {c["label"] for c in compiled.changes if c["@type"] == "top:Room"} == {"Kitchen / Dining / Edge", "Space 1"}


def test_representative_point_is_inside_an_l_shaped_room():
    ell = [(0, 0), (6000, 0), (6000, 1500), (1500, 1500), (1500, 6000), (0, 6000)]
    p = representative_point(ell)
    assert point_in_polygon(p, ell)
    cx = sum(x for x, _ in ell) / len(ell)
    cy = sum(y for _, y in ell) / len(ell)
    assert not point_in_polygon((cx + 800, cy + 800), ell)  # a naive centre would miss


def test_persistent_room_ids_survive_a_shape_change():
    data = reference_trace()
    first = regularize_trace(data)
    previous = {r.room_id: r.clear for r in first.rooms}
    moved = copy.deepcopy(data)
    for n in moved["walls_graph"]["nodes"]:  # move the int-v1 wall 200 mm east
        if n["id"].startswith("int-v1"):
            n["x"] += 200
    second = regularize_trace(moved, previous_rooms=previous)
    a = {r.label: (r.room_id, r.geometry_hash) for r in first.rooms}
    b = {r.label: (r.room_id, r.geometry_hash) for r in second.rooms}
    assert a["Living"][0] == b["Living"][0] and a["Living"][1] != b["Living"][1]
    assert a["Kitchen"] == b["Kitchen"]
    assert len({v[0] for v in b.values()}) == len(b)


# ------------------------------------------------------------------ compile / quality
@pytest.mark.parametrize("noise", [0.0, 20.0])
def test_canonical_round_trip_reproduces_the_reference_plan(noise):
    trace = TraceResult.model_validate(reference_trace(noise_mm=noise, split_one_wall=True))
    compiled = compile_trace(trace, representation="native_aec", geometry_pipeline="canonical")
    assert compiled.pipeline == "canonical" and compiled.warnings == []
    rooms = {c["label"]: c["geometry"]["boundary"] for c in compiled.changes if c["@type"] == "top:Room"}
    assert set(rooms) == set(ROOMS)
    for name, (xl, xr, yb, yt) in ROOMS.items():
        xs = [p[0] for p in rooms[name]]
        ys = [p[1] for p in rooms[name]]
        assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((xl - X0, xr - X0, yb - Y0, yt - Y0), abs=1.0 + noise)
    walls = {c["@id"]: c["geometry"] for c in compiled.changes if c["@type"] == "top:Wall"}
    ops = [c for c in compiled.changes if c["@type"] in ("top:Door", "top:Window")]
    assert len(ops) == 26
    for change, (wall, off, width) in zip(ops, DOORS + WINDOWS):
        g = change["geometry"]
        w = walls[g["host_wall_id"]]
        L = math.dist(w["start"], w["end"])
        s = g["offset"] + g["width"] / 2
        c = (w["start"][0] + (w["end"][0] - w["start"][0]) * s / L + X0,
             w["start"][1] + (w["end"][1] - w["start"][1]) * s / L + Y0)
        assert g["width"] == width
        assert c == pytest.approx(_opening_center(wall, off, width), abs=2.0 + 2 * noise)
    from topology_engine.models import TopologyChange

    for c in compiled.changes:
        TopologyChange.model_validate(c)


def test_compile_accepts_a_regularized_plan_and_rejects_unknown_pipelines():
    reg = regularize_trace(reference_trace())
    assert compile_trace(reg).summary()["counts"]["room"] == 12
    with pytest.raises(ValueError, match="geometry_pipeline"):
        compile_trace(TraceResult.model_validate(reference_trace()), geometry_pipeline="fast")


def test_canonical_door_orientation_flips_with_a_reversed_host():
    data = reference_trace()
    data["doors_list"][0]["edge_nodes"] = ["ext-s-0-b", "ext-s-0-a"]
    data["doors_list"][0].update(hinge="start", swing="right")
    compiled = compile_trace(TraceResult.model_validate(data), geometry_pipeline="canonical")
    door = next(c for c in compiled.changes if c["label"] == "Door 1")
    wall = next(c for c in compiled.changes if c["@id"] == door["geometry"]["host_wall_id"])
    assert wall["geometry"]["start"][0] < wall["geometry"]["end"][0]
    assert door["geometry"]["hinge"] == "right" and door["geometry"]["swing"] == "in"


def test_uncalibrated_scale_is_visible_in_diagnostics():
    data = _as_pixels(reference_trace())
    data["frame"]["calibrated"] = False
    data["frame"]["scale_source"] = "nominal 230 mm wall thickness"
    reg = regularize_trace(data)
    assert "scale_uncalibrated" in _codes(reg)


def test_quality_indicators_prefer_the_complete_plan():
    good = compile_trace(TraceResult.model_validate(reference_trace()), geometry_pipeline="canonical")
    broken_data = reference_trace()
    edges = broken_data["walls_graph"]["edges"]
    broken_data["walls_graph"]["edges"] = [e for i, e in enumerate(edges) if i not in (1, 2)]  # east + north missing
    broken = compile_trace(TraceResult.model_validate(broken_data), geometry_pipeline="canonical")
    qa, qb = assess(good, [("Kitchen", 393.5, 439.5)]), assess(broken)
    assert qa["dangling_ratio"] == 0 and qa["opening_host_rate"] == 1.0
    assert qa["size_agreement"] > 0.95
    assert qa["score"] > 0.9 > qb["score"]
    legacy = compile_trace(TraceResult.model_validate(reference_trace()))
    assert assess(legacy)["rooms"] == 12


def test_canonical_trace_result_round_trip():
    reg = regularize_trace(reference_trace())
    tr = to_trace_result(normalize(reference_trace()))
    assert tr.schema_version == 2 and len(tr.doors_list) == 13
    again = compile_trace(tr, geometry_pipeline="canonical")
    assert again.summary()["counts"] == compile_trace(reg).summary()["counts"]
