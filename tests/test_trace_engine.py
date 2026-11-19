"""Tests for the trace pipeline: graph cleanup, room recovery, compilation.

The reference plan is the 2,000 sq ft (50 ft x 40 ft) 3-bedroom layout
generated on 31 Oct 2026: 15 wall centerlines, 12 rooms, 13 doors, 13 windows.
"""

from __future__ import annotations

import math
import random

import pytest

from trace_engine import TraceResult, compile_trace
from trace_engine.graph import clean
from trace_engine.rooms import find_faces, point_in_polygon, signed_area

EXT, INT = 230, 150
X0, X3, Y0, Y3 = 115.0, 15125.0, 115.0, 12077.0

WALLS = {
    "ext-s": ((X0, Y0), (X3, Y0), EXT), "ext-e": ((X3, Y0), (X3, Y3), EXT),
    "ext-n": ((X3, Y3), (X0, Y3), EXT), "ext-w": ((X0, Y3), (X0, Y0), EXT),
    "int-h1": ((X0, 5300), (11000, 5300), INT), "int-h2": ((X0, 6650), (X3, 6650), INT),
    "int-h3": ((11000, 4700), (X3, 4700), INT), "int-h4": ((4000, 9000), (5800, 9000), INT),
    "int-h5": ((13000, 9000), (X3, 9000), INT), "int-v1": ((6200, Y0), (6200, 5300), INT),
    "int-v2": ((11000, Y0), (11000, 6650), INT), "int-v3": ((4000, 6650), (4000, Y3), INT),
    "int-v4": ((5800, 6650), (5800, Y3), INT), "int-v5": ((9700, 6650), (9700, Y3), INT),
    "int-v6": ((13000, 6650), (13000, Y3), INT),
}
ROOMS = {  # name: clear rectangle (xl, xr, yb, yt)
    "Living": (230, 6125, 230, 5225), "Dining": (6275, 10925, 230, 5225),
    "Kitchen": (11075, 15010, 230, 4625), "Utility": (11075, 15010, 4775, 6575),
    "Passage": (230, 10925, 5375, 6575), "Bedroom 2": (230, 3925, 6725, 11962),
    "Common Bath": (4075, 5725, 6725, 8925), "Bath 2": (4075, 5725, 9075, 11962),
    "Bedroom 3": (5875, 9625, 6725, 11962), "Master Bedroom": (9775, 12925, 6725, 11962),
    "Master Bath": (13075, 15010, 6725, 8925), "Dressing": (13075, 15010, 9075, 11962),
}
DOORS = [  # host wall, offset of opening start, width
    ("ext-s", 2485, 1200), ("int-v1", 1385, 1800), ("int-v2", 1885, 900),
    ("int-h3", 2800, 800), ("int-h1", 6485, 1000), ("int-h2", 2785, 900),
    ("int-h2", 4285, 750), ("int-h2", 8485, 900), ("int-h2", 9835, 900),
    ("int-v3", 3550, 750), ("int-v6", 750, 750), ("int-v6", 3350, 900),
    ("ext-e", 5185, 900),
]
WINDOWS = [
    ("ext-s", 4185, 1500), ("ext-w", 8577, 1500), ("ext-s", 7585, 1800),
    ("ext-s", 12185, 1500), ("ext-e", 1385, 1200), ("ext-n", 12325, 1500),
    ("ext-w", 2377, 1200), ("ext-n", 10025, 600), ("ext-n", 6625, 1500),
    ("ext-n", 3025, 1500), ("ext-e", 7485, 600), ("ext-e", 10085, 900),
    ("ext-w", 5652, 900),
]


def _opening_center(wall: str, offset: float, width: float):
    (a, b, _t) = WALLS[wall]
    length = math.dist(a, b)
    s = offset + width / 2
    return (a[0] + (b[0] - a[0]) * s / length, a[1] + (b[1] - a[1]) * s / length)


def reference_trace(noise_mm: float = 0.0, split_one_wall: bool = False, seed: int = 7) -> dict:
    """The reference plan expressed as a recogniser would report it.

    Each wall gets its own two end nodes (as raw detectors produce), so the
    cleanup has to merge shared corners and split walls at T-junctions.
    """
    rng = random.Random(seed)

    def jitter(p):
        return (p[0] + rng.uniform(-noise_mm, noise_mm), p[1] + rng.uniform(-noise_mm, noise_mm))

    nodes, edges = [], []
    for name, (a, b, t) in WALLS.items():
        kind = "external" if name.startswith("ext") else "internal"
        if split_one_wall and name == "int-h2":
            mid = (8000.0, 6650.0)
            pieces = [(a, mid), (mid, b)]
        else:
            pieces = [(a, b)]
        for k, (p, q) in enumerate(pieces):
            ida, idb = f"{name}-{k}-a", f"{name}-{k}-b"
            pa, pb = jitter(p), jitter(q)
            nodes += [{"id": ida, "x": pa[0], "y": pa[1]}, {"id": idb, "x": pb[0], "y": pb[1]}]
            edges.append({"source": ida, "target": idb, "type": kind, "thickness": t})
    doors = [
        {"edge_nodes": [f"{w}-0-a", f"{w}-0-b"], "length": width,
         "center": list(_opening_center(w, off, width))}
        for w, off, width in DOORS
    ]
    windows = [
        {"edge_nodes": [f"{w}-0-a", f"{w}-0-b"], "length": width,
         "distance_from_lower_node": off + width / 2}
        for w, off, width in WINDOWS
    ]
    zones = [
        {"name": name, "seedPoint": [(xl + xr) / 2, (yb + yt) / 2]}
        for name, (xl, xr, yb, yt) in ROOMS.items()
    ]
    return {
        "walls_graph": {"nodes": nodes, "edges": edges},
        "doors_list": doors,
        "windows_list": windows,
        "zones_list": zones,
        "units": "mm",
    }


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), max(xs), min(ys), max(ys))


# ------------------------------------------------------------------ schema
def test_pixel_trace_requires_calibration():
    data = reference_trace()
    data["units"] = "px"
    with pytest.raises(ValueError, match="mm_per_unit"):
        TraceResult.model_validate(data)


def test_edges_must_reference_known_nodes():
    data = reference_trace()
    data["walls_graph"]["edges"][0]["target"] = "nope"
    with pytest.raises(ValueError, match="unknown node"):
        TraceResult.model_validate(data)


# ----------------------------------------------------------------- cleanup
def test_cleanup_merges_corners_and_splits_t_junctions():
    network = clean(TraceResult.model_validate(reference_trace()))
    # 4 corners + every T-junction becomes one shared node.
    degrees = network.degree()
    assert all(d >= 2 for d in degrees.values())
    assert sum(1 for d in degrees.values() if d == 3) > 10
    # Total wall length is preserved by splitting.
    total = sum(network.length(w) for w in network.walls)
    expected = sum(math.dist(a, b) for a, b, _ in WALLS.values())
    assert total == pytest.approx(expected, rel=1e-6)


def test_cleanup_straightens_noisy_walls_and_rejoins_split_runs():
    noisy = clean(TraceResult.model_validate(reference_trace(noise_mm=25, split_one_wall=True)))
    clean_ref = clean(TraceResult.model_validate(reference_trace()))
    assert len(noisy.walls) == len(clean_ref.walls)
    for wall in noisy.walls:
        (x1, y1), (x2, y2) = noisy.nodes[wall.a], noisy.nodes[wall.b]
        assert x1 == pytest.approx(x2) or y1 == pytest.approx(y2)  # exactly orthogonal


# ------------------------------------------------------------------- rooms
def test_faces_are_the_twelve_rooms():
    network = clean(TraceResult.model_validate(reference_trace()))
    faces = find_faces(network)
    assert len(faces) == 12
    assert all(signed_area([network.nodes[n] for n in cycle]) > 0 for cycle, _ in faces)


def test_point_in_polygon():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_polygon((5, 5), square)
    assert not point_in_polygon((15, 5), square)


# ------------------------------------------------------------- round trip
@pytest.mark.parametrize("noise", [0.0, 20.0])
def test_round_trip_reproduces_the_reference_plan(noise):
    trace = TraceResult.model_validate(reference_trace(noise_mm=noise, split_one_wall=True))
    compiled = compile_trace(trace, representation="native_aec")

    assert compiled.warnings == []
    rooms = {c["label"]: c["geometry"]["boundary"] for c in compiled.changes if c["@type"] == "top:Room"}
    assert set(rooms) == set(ROOMS)
    # Clear interiors match the reference rectangles (shifted so the
    # footprint's lower-left wall-centre corner sits at the origin).
    shift = (-X0, -Y0)
    for name, (xl, xr, yb, yt) in ROOMS.items():
        got = _bbox(rooms[name])
        want = (xl + shift[0], xr + shift[0], yb + shift[1], yt + shift[1])
        assert got == pytest.approx(want, abs=1.0 + noise), name
        assert len(rooms[name]) == 4, name

    walls = {c["@id"]: c["geometry"] for c in compiled.changes if c["@type"] == "top:Wall"}

    def world_center(change):
        g = change["geometry"]
        w = walls[g["host_wall_id"]]
        a, b = w["start"], w["end"]
        length = math.dist(a, b)
        s = g["offset"] + g["width"] / 2
        return (a[0] + (b[0] - a[0]) * s / length - shift[0], a[1] + (b[1] - a[1]) * s / length - shift[1])

    doors = [c for c in compiled.changes if c["@type"] == "top:Door"]
    windows = [c for c in compiled.changes if c["@type"] == "top:Window"]
    assert len(doors) == 13 and len(windows) == 13
    for change, (wall, off, width) in zip(doors + windows, DOORS + WINDOWS):
        assert change["geometry"]["width"] == width
        assert world_center(change) == pytest.approx(_opening_center(wall, off, width), abs=2.0 + 2 * noise)


def test_unlabelled_loops_are_named_and_reported():
    data = reference_trace()
    data["zones_list"] = data["zones_list"][:-2]
    compiled = compile_trace(TraceResult.model_validate(data))
    labels = [c["label"] for c in compiled.changes if c["@type"] == "top:Room"]
    assert sum(label.startswith("Space ") for label in labels) == 2
    assert any("no room label" in w for w in compiled.warnings)


def test_pixel_trace_is_scaled_and_flipped():
    data = reference_trace()
    # Express the same plan in y-down pixels at 20 mm per pixel.
    for node in data["walls_graph"]["nodes"]:
        node["x"], node["y"] = node["x"] / 20, -node["y"] / 20
    for door in data["doors_list"]:
        door["center"] = [door["center"][0] / 20, -door["center"][1] / 20]
        door["length"] /= 20
    for window in data["windows_list"]:
        window["distance_from_lower_node"] /= 20
        window["length"] /= 20
    for edge in data["walls_graph"]["edges"]:
        edge["thickness"] /= 20
    for zone in data["zones_list"]:
        zone["seedPoint"] = [zone["seedPoint"][0] / 20, -zone["seedPoint"][1] / 20]
    data.update(units="px", mm_per_unit=20.0, y_down=True)
    compiled = compile_trace(TraceResult.model_validate(data))
    assert compiled.warnings == []
    assert compiled.summary()["footprint_mm"] == [15010, 11962]
    assert len([c for c in compiled.changes if c["@type"] == "top:Room"]) == 12


def test_door_orientation_flips_when_cleanup_reverses_the_host_wall():
    data = reference_trace()
    # Describe the main door against a reversed edge (east -> west).
    data["doors_list"][0]["edge_nodes"] = ["ext-s-0-b", "ext-s-0-a"]
    data["doors_list"][0]["hinge"] = "start"
    data["doors_list"][0]["swing"] = "right"
    compiled = compile_trace(TraceResult.model_validate(data))
    door = next(c for c in compiled.changes if c["label"] == "Door 1")
    wall = next(c for c in compiled.changes if c["@id"] == door["geometry"]["host_wall_id"])
    assert wall["geometry"]["start"][0] < wall["geometry"]["end"][0]  # cleaned wall runs west->east
    assert door["geometry"]["hinge"] == "right" and door["geometry"]["swing"] == "in"


def test_label_parsing_and_size_pairing_ordering_cases():
    """Verify line building, sorting by x (including '360 380 x' ordering),
    two-line split sizes, single-use of sizes, and 50-1500 cm bounds."""
    from trace_engine.vision import parse_labels_from_data

    # 1. '360 380 x' case: 'x' is vertically slightly below '360' and '380'
    # Initial y-sort would place 'x' after '380', but sorting line words by x fixes it.
    fake_data_ordering = {
        "text": ["BED", "ROOM", "360", "380", "x"],
        "conf": [95, 95, 95, 96, 95],
        "left": [100, 160, 100, 170, 145],
        "top": [50, 50, 80, 80, 85],  # 'x' has top 85 > 80
        "width": [50, 50, 40, 40, 15],
        "height": [20, 20, 20, 20, 15],
    }
    labels = parse_labels_from_data(fake_data_ordering)
    assert len(labels) == 1
    assert labels[0].text == "Bed Room"
    assert labels[0].size_cm == (360.0, 380.0)

    # 2. Two-line split size: "330 x" on one line, "360" on the next line
    fake_data_split = {
        "text": ["KITCHEN", "330", "x", "360"],
        "conf": [95, 95, 95, 95],
        "left": [100, 100, 145, 100],
        "top": [100, 116, 116, 132],  # within 2.5 text heights of top 100 (h=16)
        "width": [80, 40, 15, 40],
        "height": [16, 16, 16, 16],
    }
    labels_split = parse_labels_from_data(fake_data_split)
    assert len(labels_split) == 1
    assert labels_split[0].text == "Kitchen"
    assert labels_split[0].size_cm == (330.0, 360.0)

    # 3. Reject sizes outside 50–1500 cm per side
    fake_data_bounds = {
        "text": ["STORE", "40", "x", "30", "WAREHOUSE", "2000", "x", "3000"],
        "conf": [95, 95, 95, 95, 95, 95, 95, 95],
        "left": [100, 100, 130, 150, 400, 400, 460, 480],
        "top": [100, 125, 125, 125, 100, 125, 125, 125],
        "width": [60, 25, 15, 25, 100, 50, 15, 50],
        "height": [16, 16, 16, 16, 16, 16, 16, 16],
    }
    labels_bounds = parse_labels_from_data(fake_data_bounds)
    assert len(labels_bounds) == 2
    assert labels_bounds[0].size_cm is None
    assert labels_bounds[1].size_cm is None

    # 4. Each size line is used at most once and attached to the nearest name line directly above it
    fake_data_single_use = {
        "text": ["BATH", "ROOM", "COMMON", "BATH", "150", "x", "250"],
        "conf": [95, 95, 95, 95, 95, 95, 95],
        "left": [100, 150, 100, 180, 100, 140, 160],
        "top": [80, 80, 110, 110, 130, 130, 130],
        "width": [45, 45, 70, 45, 35, 15, 35],
        "height": [16, 16, 16, 16, 16, 16, 16],
    }
    labels_single = parse_labels_from_data(fake_data_single_use)
    assert len(labels_single) == 2
    assert labels_single[0].text == "Bath Room" and labels_single[0].size_cm is None
    assert labels_single[1].text == "Common Bath" and labels_single[1].size_cm == (150.0, 250.0)


def test_label_parsing_feet_inches_metres_and_cm():
    """Verify parsing of feet-inches (12'x13', 12'6"x10'), metres (3.30X3.40, 3.3 x 3.4),
    and centimetres, converted to cm and bounded within 50-1500 cm."""
    from trace_engine.vision import parse_size, parse_labels_from_data

    # Direct parse_size unit tests
    assert parse_size("12'x13'") == (365.8, 396.2)
    assert parse_size("12'6\"x10'") == (381.0, 304.8)
    assert parse_size("12'-6\" x 10'-0\"") == (381.0, 304.8)
    assert parse_size("12’6” x 10’") == (381.0, 304.8)
    assert parse_size("12'6'' x 10'") == (381.0, 304.8)
    assert parse_size("3.30X3.40") == (330.0, 340.0)
    assert parse_size("3.3 x 3.4") == (330.0, 340.0)
    assert parse_size("3.30m x 3.40m") == (330.0, 340.0)
    assert parse_size("3,30 x 3,40") == (330.0, 340.0)
    assert parse_size("355 x 400") == (355.0, 400.0)
    assert parse_size("360 X 380") == (360.0, 380.0)
    assert parse_size("3300mm x 3400mm") == (330.0, 340.0)

    # Sanity bounds rejection (50-1500 cm)
    assert parse_size("60' x 80'") is None  # 1828 x 2438 cm > 1500 cm
    assert parse_size("1' x 1'") is None   # 30.5 x 30.5 cm < 50 cm
    assert parse_size("20.0 x 30.0") is None  # 2000 x 3000 cm > 1500 cm
    assert parse_size("0.3 x 0.4") is None    # 30 x 40 cm < 50 cm

    # Fake OCR words integration test
    text_items = [
        # Room 1: feet-inches with detached quote/words
        "MASTER", "BEDROOM", "12'6\"", "x", "10'",
        # Room 2: metres with capital X
        "LIVING", "ROOM", "3.30X3.40",
        # Room 3: metres with spaces and decimal
        "DINING", "3.3", "x", "3.4",
        # Room 4: centimetres
        "KITCHEN", "355", "x", "400",
        # Room 5: out-of-bounds feet
        "TOWER", "60'", "x", "70'",
        # Room 6: out-of-bounds metres
        "HALL", "25.0", "x", "30.0",
    ]
    fake_ocr_data = {
        "text": text_items,
        "conf": [95] * len(text_items),
        "left": [
            # Room 1
            50, 110, 50, 100, 115,
            # Room 2
            300, 360, 300,
            # Room 3
            500, 500, 530, 545,
            # Room 4
            700, 700, 735, 750,
            # Room 5
            50, 50, 80, 95,
            # Room 6
            300, 300, 345, 360,
        ],
        "top": [
            # Room 1: name at 50, size at 75
            50, 50, 75, 75, 75,
            # Room 2: name at 50, size at 75
            50, 50, 75,
            # Room 3: name at 50, size at 75
            50, 75, 75, 75,
            # Room 4: name at 50, size at 75
            50, 75, 75, 75,
            # Room 5: name at 200, size at 225
            200, 225, 225, 225,
            # Room 6: name at 200, size at 225
            200, 225, 225, 225,
        ],
        "width": [
            # Room 1
            55, 70, 45, 12, 25,
            # Room 2
            55, 45, 80,
            # Room 3
            60, 25, 12, 25,
            # Room 4
            65, 30, 12, 30,
            # Room 5
            55, 25, 12, 25,
            # Room 6
            50, 40, 12, 40,
        ],
        "height": [16] * len(text_items),
    }

    labels = parse_labels_from_data(fake_ocr_data)
    by_name = {l.text: l for l in labels}

    assert by_name["Master Bedroom"].size_cm == (381.0, 304.8)
    assert by_name["Living Room"].size_cm == (330.0, 340.0)
    assert by_name["Dining"].size_cm == (330.0, 340.0)
    assert by_name["Kitchen"].size_cm == (355.0, 400.0)
    assert by_name["Tower"].size_cm is None
    assert by_name["Hall"].size_cm is None


def test_no_walls_found_returns_clean_no_draft(tmp_path):
    """When an image has no wall fill/structure, trace_image and compile_trace
    must return a clean no-draft result rather than raising ValueError."""
    from PIL import Image
    from trace_engine.vision import trace_image, render_overlay

    blank_path = tmp_path / "blank.png"
    Image.new("RGB", (400, 400), (255, 255, 255)).save(blank_path)

    # Classic engine on a blank image has no walls
    trace, report = trace_image(str(blank_path), engine="classic")
    assert len(trace.walls_graph.edges) == 0
    assert len(trace.walls_graph.nodes) == 0
    assert "no walls found" in report.warnings

    compiled = compile_trace(trace)
    summary = compiled.summary()
    assert summary["counts"] == {}
    assert summary["rooms"] == []
    assert any("no walls" in w for w in summary["warnings"] + compiled.warnings)

    overlay_out = tmp_path / "overlay.png"
    render_overlay(str(blank_path), trace, compiled, str(overlay_out))
    assert overlay_out.is_file()


def test_nan_calibration_and_coordinates_returns_clean_draft():
    """calibrate_scale with non-matching samples and traces with NaN scale
    or coordinates must never crash with 'ValueError: cannot convert float NaN to integer'."""
    from trace_engine.models import TraceNode, TraceEdge, WallsGraph
    from trace_engine.vision import calibrate_scale, Label

    # 1. calibrate_scale with samples whose mm_per_px differ widely (used list empty)
    trace_stub = TraceResult(
        walls_graph=WallsGraph(nodes=[], edges=[]),
        units="mm", mm_per_unit=1.0, y_down=True, source="test",
    )
    labels = [
        Label(text="Bed", box=(10, 10, 50, 50), size_cm=(300.0, 400.0)),
        Label(text="Living", box=(100, 100, 50, 50), size_cm=(600.0, 800.0)),
    ]
    scale, method, samples = calibrate_scale(trace_stub, labels, thickness_px=10.0, nominal_wall_mm=230.0)
    assert math.isfinite(scale) and scale > 0
    assert "nominal" in method

    # 2. compile_trace on trace with NaN mm_per_unit via model_copy
    base_trace = TraceResult(
        walls_graph=WallsGraph(
            nodes=[TraceNode(id="n1", x=10.0, y=10.0), TraceNode(id="n2", x=100.0, y=10.0)],
            edges=[TraceEdge(source="n1", target="n2", type="internal", thickness=10.0)],
        ),
        units="px", mm_per_unit=10.0, y_down=True, source="test",
    )
    nan_trace = base_trace.model_copy(update={"mm_per_unit": float("nan")})
    compiled_nan = compile_trace(nan_trace)
    assert compiled_nan is not None
    assert len(compiled_nan.changes) > 0

    # 3. compile_trace on trace with NaN coordinate
    nan_coord_trace = TraceResult(
        walls_graph=WallsGraph(
            nodes=[TraceNode(id="n1", x=float("nan"), y=10.0), TraceNode(id="n2", x=100.0, y=10.0)],
            edges=[TraceEdge(source="n1", target="n2", type="internal", thickness=10.0)],
        ),
        units="px", mm_per_unit=10.0, y_down=True, source="test",
    )
    compiled_coord = compile_trace(nan_coord_trace)
    assert compiled_coord is not None


