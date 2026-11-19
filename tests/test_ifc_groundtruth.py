"""IFC ground truth -> registration -> training sample, on a synthetic IFC.

The synthetic file mimics the exporter the real data comes from: walls as
rectangular footprints that only meet face-to-face, wall bodies split into
several extrusions around openings, openings linked by IfcRelVoidsElement /
IfcRelFillsElement, spaces aggregated to the storey. Real-data checks run
only when dataset/raw_vector_exports is present.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_engine.gt_dataset import accept_samples, junction_type, write_review_page, write_sample  # noqa: E402
from trace_engine.ifc_groundtruth import connect_ends, footprint_to_centerline, load_storeys, Segment  # noqa: E402
from trace_engine.ifc_step import IfcFile, _parse_args  # noqa: E402
from trace_engine.register import IMAGE_FRAME_POSES, load_image, match_folder, register  # noqa: E402

REAL = ROOT / "dataset" / "raw_vector_exports" / "PDF plan" / "1 FIRST 6"


# ------------------------------------------------------------ synthetic IFC
class _Step:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.n = 0

    def add(self, text: str) -> str:
        self.n += 1
        self.lines.append(f"#{self.n}={text};")
        return f"#{self.n}"

    def pt(self, x: float, y: float, z: float | None = None) -> str:
        return self.add(f"IFCCARTESIANPOINT(({x:.1f},{y:.1f}{'' if z is None else f',{z:.1f}'}))")

    def axis(self, x=0.0, y=0.0, z=0.0) -> str:
        return self.add(f"IFCAXIS2PLACEMENT3D({self.pt(x, y, z)},{self.zdir},{self.xdir})")

    def prism(self, poly, depth=3000.0, z=0.0) -> str:
        pts = [self.pt(x, y) for x, y in list(poly) + [poly[0]]]
        line = self.add(f"IFCPOLYLINE(({','.join(pts)}))")
        prof = self.add(f"IFCARBITRARYCLOSEDPROFILEDEF(.AREA.,$,{line})")
        return self.add(f"IFCEXTRUDEDAREASOLID({prof},{self.axis(0, 0, z)},{self.zdir},{depth:.1f})")

    def shape(self, items) -> str:
        rep = self.add(f"IFCSHAPEREPRESENTATION($,'Body','SweptSolid',({','.join(items)}))")
        return self.add(f"IFCPRODUCTDEFINITIONSHAPE($,$,({rep}))")

    def text(self) -> str:
        head = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');\n"
                "FILE_NAME('t.ifc','1970-01-01T00:00:00',('test'),('test'),'test','test','test');\n"
                "FILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\n")
        return head + "\n".join(self.lines) + "\nENDSEC;\nEND-ISO-10303-21;\n"


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def synthetic_ifc(path: Path) -> Path:
    """6 x 4 m house (centrelines), 200 mm walls, one partition at x=3000,
    a door in the partition and a window in the bottom wall."""
    s = _Step()
    s.add("IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.)")
    s.zdir = s.add("IFCDIRECTION((0.,0.,1.))")
    s.xdir = s.add("IFCDIRECTION((1.,0.,0.))")
    site_pl = s.add(f"IFCLOCALPLACEMENT($,{s.axis()})")
    st_pl = s.add(f"IFCLOCALPLACEMENT({site_pl},{s.axis(0, 0, 0)})")
    storey = s.add(f"IFCBUILDINGSTOREY('g1',$,'Ground floor',$,$,{st_pl},$,$,.ELEMENT.,0.)")
    t = 200.0
    walls = {
        # horizontal walls run over the corners; verticals stop at their faces
        "bottom": _rect(-100, -100, 6100, 100),
        "top": _rect(-100, 3900, 6100, 4100),
        "left": _rect(-100, 100, 100, 3900),
        "right": _rect(5900, 100, 6100, 3900),
        "mid": _rect(2900, 100, 3100, 3900),
    }
    ids = {}
    for name, poly in walls.items():
        items = [s.prism(poly)]
        if name == "bottom":  # body split around the window like the real exporter
            items = [s.prism(_rect(-100, -100, 900, 100)), s.prism(_rect(2100, -100, 6100, 100)),
                     s.prism(_rect(900, -100, 2100, 100), depth=900)]
        pl = s.add(f"IFCLOCALPLACEMENT({st_pl},{s.axis()})")
        ids[name] = s.add(f"IFCWALL('w{name}',$,'Wall',$,$,{pl},{s.shape(items)},'shape:{name}',.STANDARD.)")
    # door in the partition, window in the bottom wall
    pl = s.add(f"IFCLOCALPLACEMENT({st_pl},{s.axis()})")
    op_d = s.add(f"IFCOPENINGELEMENT('od',$,'Door Opening',$,$,{pl},{s.shape([s.prism(_rect(2850, 1550, 3150, 2450), 2100)])},'d:opening',.OPENING.)")
    door = s.add(f"IFCDOOR('d1',$,'Door',$,$,{pl},{s.shape([s.prism(_rect(2950, 1550, 3050, 2450), 2100)])},'shape:d1',2100.,900.,.DOOR.,.NOTDEFINED.,$)")
    op_w = s.add(f"IFCOPENINGELEMENT('ow',$,'Window Opening',$,$,{pl},{s.shape([s.prism(_rect(900, -150, 2100, 150), 1200, 900)])},'w:opening',.OPENING.)")
    win = s.add(f"IFCWINDOW('wi1',$,'Window',$,$,{pl},{s.shape([s.prism(_rect(900, -50, 2100, 50), 1200, 900)])},'shape:wi1',1200.,1200.,.WINDOW.,.NOTDEFINED.,$)")
    s.add(f"IFCRELVOIDSELEMENT('v1',$,$,$,{ids['mid']},{op_d})")
    s.add(f"IFCRELFILLSELEMENT('f1',$,$,$,{op_d},{door})")
    s.add(f"IFCRELVOIDSELEMENT('v2',$,$,$,{ids['bottom']},{op_w})")
    s.add(f"IFCRELFILLSELEMENT('f2',$,$,$,{op_w},{win})")
    spaces = []
    for gid, long_name, poly in (("s1", "Bed Room", _rect(100, 100, 2900, 3900)),
                                 ("s2", "Living", _rect(3100, 100, 5900, 3900))):
        pl = s.add(f"IFCLOCALPLACEMENT({st_pl},{s.axis()})")
        spaces.append(s.add(f"IFCSPACE('{gid}',$,'{gid}',$,$,{pl},{s.shape([s.prism(poly, 2800)])},'{long_name}',.ELEMENT.,.SPACE.,0.)"))
    s.add(f"IFCRELCONTAINEDINSPATIALSTRUCTURE('c1',$,$,$,({','.join(list(ids.values()) + [door, win])}),{storey})")
    s.add(f"IFCRELAGGREGATES('a1',$,$,$,{storey},({','.join(spaces)}))")
    path.write_text(s.text(), encoding="latin-1")
    return path


# ------------------------------------------------------------ STEP parsing
def test_step_parser_handles_strings_enums_and_nesting():
    args, _ = _parse_args("('a''b',$,.T.,(1,2.5,-3.E2),#12,IFCLABEL('x'),*)", 0)
    assert args[0] == "a'b" and args[1] is None and args[2] == "T"
    assert args[3] == [1, 2.5, -300.0] and int(args[4]) == 12
    assert args[5].type == "IFCLABEL" and args[5].value == "x" and args[6] is None


def test_footprint_centerline_square_and_mitred():
    a, b, t = footprint_to_centerline(_rect(0, -100, 4000, 100))
    assert t == pytest.approx(200) and {a, b} == {(0.0, 0.0), (4000.0, 0.0)}
    # mitred end: the centreline runs to the miter point on the axis
    a, b, t = footprint_to_centerline([(0, -100), (4000, -100), (4200, 100), (-200, 100)])
    assert t == pytest.approx(200)
    assert sorted([a[0], b[0]]) == pytest.approx([-100, 4100])


def test_connect_ends_reaches_the_abutting_centreline():
    through = Segment((0, 0), (4000, 0), 200, "wall")
    stub = Segment((2000, 100), (2000, 3000), 200, "wall")  # stops at the face
    out = connect_ends([through, stub])
    assert out[1].a == pytest.approx((2000, 0))


# ------------------------------------------------------------ storey build
def test_synthetic_storey_is_one_connected_graph(tmp_path):
    ifc = synthetic_ifc(tmp_path / "house.ifc")
    assert IfcFile.open(ifc).length_scale == 1.0
    (gt,) = load_storeys(ifc)
    st = gt.stats
    assert st["components"] == 1 and st["dead_ends"] == 0
    assert st["walls"] == 7  # 4 outer split at the partition T's + partition
    assert st["external"] == 6
    assert st["doors"] == 1 and st["windows"] == 1 and st["openings_unhosted"] == 0
    assert st["spaces"] == 2 and st["spaces_matched"] == 2
    # the split wall body (window) is read as one full-length wall
    bottom = [p for k, p in gt.wall_polygons if min(y for _, y in p) < -50]
    assert max(x for x, _ in bottom[0]) - min(x for x, _ in bottom[0]) == pytest.approx(6200)
    door = gt.trace.doors_list[0]
    assert door.length == pytest.approx(900) and door.center == pytest.approx((3000, 2000))
    names = sorted(z.name for z in gt.trace.zones_list)
    assert names == ["Bed Room", "Living"]


def _draw(gt, A, size=(900, 700)):
    from PIL import Image, ImageDraw

    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    to = lambda p: (A[0][0] * p[0] + A[0][1] * p[1] + A[0][2], A[1][0] * p[0] + A[1][1] * p[1] + A[1][2])  # noqa: E731
    for kind, poly in gt.wall_polygons:
        d.polygon([to(p) for p in poly], outline=0, width=2)
    for op in gt.openings:  # doors are gaps in a drawing
        if op.kind == "door":
            d.polygon([to(p) for p in op.polygon], fill=255)
    d.text((20, 20), "GROUND FLOOR", fill=0)
    return im


def test_register_recovers_the_image_transform(tmp_path):
    (gt,) = load_storeys(synthetic_ifc(tmp_path / "house.ifc"))
    A = [[0.11, 0.0, 120.0], [0.0, 0.11, 140.0]]  # image-frame pose: y down the image
    path = tmp_path / "plan.png"
    _draw(gt, A).save(path)
    reg = register(load_image(path), gt, poses=IMAGE_FRAME_POSES)
    assert reg is not None and reg.mirror and reg.rotation == 0
    for corner in [(-100, -100), (6100, 4100), (3000, 2000)]:
        got = reg.to_px(corner)
        want = (A[0][0] * corner[0] + A[0][2], A[1][1] * corner[1] + A[1][2])
        assert math.dist(got, want) < 3.0
    assert reg.support > 0.8


def test_match_folder_assigns_and_writes_sample(tmp_path):
    (gt,) = load_storeys(synthetic_ifc(tmp_path / "house.ifc"))
    A = [[0.11, 0.0, 120.0], [0.0, 0.11, 140.0]]
    img = tmp_path / "plan.png"
    _draw(gt, A).save(img)
    blank = tmp_path / "blank.png"
    from PIL import Image

    Image.new("L", (600, 600), 255).save(blank)
    regs, score = match_folder([img, blank], [gt], log=None)
    assert [Path(r.image).name for r in regs] == ["plan.png"]
    reg = regs[0]
    meta = write_sample(gt, reg, tmp_path / "cand" / "s1", "Folder A")
    assert meta["annotation_complete"] is False and meta["group_id"] == "Folder_A"
    rooms = np.asarray(Image.open(tmp_path / "cand" / "s1" / "rooms.png"))
    icons = np.asarray(Image.open(tmp_path / "cand" / "s1" / "icons.png"))
    assert rooms.shape == (700, 900) and (rooms == 2).sum() > 1000  # walls
    assert set(np.unique(icons)) >= {0, 1, 2}
    graph = json.loads((tmp_path / "cand" / "s1" / "graph.json").read_text())
    assert {n["type"] for n in graph["graph_px"]["nodes"]} >= {"L", "T"}
    assert write_review_page(tmp_path / "cand", tmp_path / "review.html") == 1
    res = accept_samples(tmp_path / "cand", tmp_path / "train", ["s1"], val_groups=[])
    m = json.loads((tmp_path / "train" / "s1" / "meta.json").read_text())
    assert res["copied"] == ["s1"] and m["annotation_complete"] is True and m["split"] == "train"


def test_junction_types():
    r = math.radians
    assert junction_type([0.0]) == "end"
    assert junction_type([0.0, r(180)]) == "straight"
    assert junction_type([0.0, r(90)]) == "L"
    assert junction_type([0.0, r(90), r(180)]) == "T"


# ------------------------------------------------------------ real data
@pytest.mark.skipif(not REAL.is_dir(), reason="real IFC exports not present")
def test_real_folder_storeys_are_connected():
    storeys = load_storeys(next(REAL.glob("*.ifc")))
    assert len(storeys) == 6
    for gt in storeys:
        assert gt.stats["openings_unhosted"] == 0
        assert gt.stats["components"] <= 3
        assert gt.stats["spaces_matched"] >= gt.stats["spaces"] - 2
