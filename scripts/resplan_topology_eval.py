"""Test A: score the TopoSpatial topology engine against ResPlan ground truth.

ResPlan (https://github.com/m-agour/ResPlan, data CC BY 4.0, code MIT) gives
vector floor plans with known rooms, doors and walls. This script feeds the
*correct* room and door geometry straight into ``TopologyEngine`` (no image
tracing), then compares the graph it builds with ResPlan's own graph:

* adjacency    - our ``top:adjacentTo`` vs every ResPlan room-room edge
                 (rooms that touch or are separated only by a wall/door)
* connectivity - our ``top:connectsTo`` vs ResPlan's walkable edges
                 (``via_door`` and ``via_opening``)

It also scores a *proposed* adjacency rule (rooms within one wall thickness
that face each other along at least ``--min-contact-mm``) so you can see what
the fix would change before touching the engine.

Run from the repository root with the project venv:

    git clone --depth 1 https://github.com/m-agour/ResPlan.git ..\\ResPlan
    .venv\\Scripts\\python.exe scripts\\resplan_topology_eval.py --resplan ..\\ResPlan --limit 200
    .venv\\Scripts\\python.exe scripts\\resplan_topology_eval.py --resplan ..\\ResPlan

Results go to Documents/TopoSpatial Exports/resplan_eval/. Only the test split
is used; never train on it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pickle
import sys
import time
import types
import zipfile
from itertools import combinations
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

ROOM_TYPES = ("living", "kitchen", "bedroom", "bathroom", "balcony", "storage", "stair")
WALKABLE = {"via_door", "via_opening"}


# ---------------------------------------------------------------- data loading
def load_plans(resplan: Path):
    """Load ResPlan.pkl, allowing only the classes the file is known to use."""
    pkl = resplan / "ResPlan.pkl"
    if not pkl.is_file():
        z = resplan / "ResPlan.zip"
        if not z.is_file():
            sys.exit(f"ResPlan.pkl / ResPlan.zip not found in {resplan}")
        print(f"unzipping {z.name} ...")
        with zipfile.ZipFile(z) as f:
            f.extract("ResPlan.pkl", resplan)
    import shapely.io

    class SafeUnpickler(pickle.Unpickler):
        def find_class(self, module, name):
            if (module, name) == ("shapely.io", "from_wkb"):
                return shapely.io.from_wkb
            if module.startswith("numpy") and name in {"scalar", "dtype", "_reconstruct", "ndarray"}:
                return super().find_class(module, name)
            raise pickle.UnpicklingError(f"blocked unexpected class {module}.{name}")

    with open(pkl, "rb") as f:
        return SafeUnpickler(f).load()


def import_resplan_utils(resplan: Path):
    """Import ResPlan's graph code; matplotlib is only used for plotting, so stub it."""
    if "matplotlib" not in sys.modules:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            mpl = types.ModuleType("matplotlib")
            mpl.pyplot = types.ModuleType("matplotlib.pyplot")
            sys.modules["matplotlib"] = mpl
            sys.modules["matplotlib.pyplot"] = mpl.pyplot
    sys.path.insert(0, str(resplan))
    import resplan_utils  # noqa: E402

    return resplan_utils


# ---------------------------------------------------------------- ground truth
def ground_truth(ru, plan):
    """ResPlan's graph (paper definition), without fallback edges or front doors."""
    g = ru.plan_to_graph(plan)
    fallback = {frozenset((u, v)) for u, v, d in g.edges(data=True) if d.get("type") == "fallback"}
    walk = {frozenset((u, v)) for u, v, d in g.edges(data=True) if d.get("type") in WALKABLE}
    g.remove_edges_from([tuple(e) for e in fallback])
    ru.add_adjacency_edges(g)
    rooms = {n for n, d in g.nodes(data=True) if d.get("type") != "front_door"}
    adj = {frozenset((u, v)) for u, v in g.edges() if u in rooms and v in rooms}
    walk = {e for e in walk if e <= rooms}
    geoms = {n: g.nodes[n]["geometry"] for n in rooms}
    return rooms, geoms, adj, walk


# ---------------------------------------------------------------- our engine
def parts(geom):
    return [p for p in getattr(geom, "geoms", [geom]) if not p.is_empty and p.geom_type == "Polygon"]


def our_graph(engine, geoms, doors, scale):
    """Build TopologyEngine nodes from ResPlan geometry (in mm) and read its relations."""
    nodes, owner = [], {}
    for rid, geom in geoms.items():
        for k, poly in enumerate(parts(geom)):
            nid = f"{rid}#{k}"
            owner[nid] = rid
            coords = [[x * scale, y * scale] for x, y in list(poly.exterior.coords)[:-1]]
            nodes.append({"@id": nid, "@type": "top:Room", "cad:geometry": {"boundary": coords}})
    for k, door in enumerate(doors):
        for j, poly in enumerate(parts(door)):
            verts = [[x * scale, y * scale] for x, y in list(poly.exterior.coords)[:-1]]
            nodes.append({"@id": f"door_{k}_{j}", "@type": "top:Door",
                          "cad:geometry": {"kind": "polyline", "vertices": verts, "closed": True}})
    adj, walk = set(), set()
    for rel in engine._build_relations(nodes):
        a, b = owner.get(rel["subject"]), owner.get(rel["object"])
        if not a or not b or a == b:
            continue
        if rel["predicate"] == "top:adjacentTo":
            adj.add(frozenset((a, b)))
        elif rel["predicate"] == "top:connectsTo":
            walk.add(frozenset((a, b)))
    return adj, walk


def proposed_adjacency(geoms, scale, wall_mm, min_contact_mm):
    """Rooms within one wall thickness that face each other along min_contact_mm."""
    out = set()
    gap, contact = wall_mm / scale, min_contact_mm / scale
    for a, b in combinations(geoms, 2):
        ga, gb = geoms[a], geoms[b]
        if ga.distance(gb) > gap:
            continue
        if ga.boundary.intersection(gb.buffer(gap)).length >= contact:
            out.add(frozenset((a, b)))
    return out


# ---------------------------------------------------------------- scoring
class Score:
    def __init__(self):
        self.tp = self.fp = self.fn = 0

    def add(self, pred, gt):
        self.tp += len(pred & gt)
        self.fp += len(pred - gt)
        self.fn += len(gt - pred)

    def as_dict(self):
        p = self.tp / max(self.tp + self.fp, 1)
        r = self.tp / max(self.tp + self.fn, 1)
        f = 2 * p * r / max(p + r, 1e-9)
        return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3),
                "tp": self.tp, "fp": self.fp, "fn": self.fn}


def isolated(rooms, edges):
    linked = {n for e in edges for n in e}
    return len(rooms - linked)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resplan", required=True, help="folder with ResPlan.zip or ResPlan.pkl, split.json, resplan_utils.py")
    ap.add_argument("--split", default="test", choices=["test", "val"])
    ap.add_argument("--limit", type=int, default=0, help="score only the first N plans (0 = all)")
    ap.add_argument("--wall-mm", type=float, default=300.0, help="proposed rule: max gap between rooms")
    ap.add_argument("--min-contact-mm", type=float, default=500.0, help="proposed rule: min facing length")
    ap.add_argument("--out", default=str(Path.home() / "Documents" / "TopoSpatial Exports" / "resplan_eval"))
    args = ap.parse_args()

    resplan = Path(args.resplan).resolve()
    ru = import_resplan_utils(resplan)
    from topology_engine.engine import TopologyEngine

    engine = TopologyEngine()  # defaults used by the MCP: snap 1 mm, max opening gap 1500 mm
    ids = {int(x) for x in json.loads((resplan / "split.json").read_text())[args.split]}
    t0 = time.time()
    plans = [p for p in load_plans(resplan) if int(p.get("id", -1)) in ids]
    plans.sort(key=lambda p: int(p["id"]))
    if args.limit:
        plans = plans[: args.limit]
    print(f"{len(plans)} {args.split} plans loaded in {time.time() - t0:.0f}s")

    s_adj, s_walk, s_prop = Score(), Score(), Score()
    rows, errors = [], []
    iso = {"gt": 0, "ours": 0, "proposed": 0, "rooms": 0}
    for i, plan in enumerate(plans, 1):
        pid = int(plan["id"])
        try:
            rooms, geoms, gt_adj, gt_walk = ground_truth(ru, plan)
            if len(rooms) < 2:
                continue
            inner = plan.get("inner")
            scale = math.sqrt(float(plan["area"]) * 1e6 / inner.area) if inner is not None and inner.area else 1.0
            doors = [d for d in ru.get_geometries(plan.get("door"))]
            o_adj, o_walk = our_graph(engine, geoms, doors, scale)
            p_adj = proposed_adjacency(geoms, scale, args.wall_mm, args.min_contact_mm)
        except Exception as exc:  # keep going; report at the end
            errors.append({"id": pid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        s_adj.add(o_adj, gt_adj)
        s_walk.add(o_walk, gt_walk)
        s_prop.add(p_adj, gt_adj)
        iso["rooms"] += len(rooms)
        iso["gt"] += isolated(rooms, gt_adj)
        iso["ours"] += isolated(rooms, o_adj)
        iso["proposed"] += isolated(rooms, p_adj)
        rows.append({"id": pid, "rooms": len(rooms), "mm_per_unit": round(scale, 1),
                     "gt_adj": len(gt_adj), "our_adj": len(o_adj), "our_adj_correct": len(o_adj & gt_adj),
                     "proposed_adj": len(p_adj), "proposed_correct": len(p_adj & gt_adj),
                     "gt_walk": len(gt_walk), "our_walk": len(o_walk), "our_walk_correct": len(o_walk & gt_walk)})
        if i % 100 == 0:
            print(f"  {i}/{len(plans)}  adjacency F1 {s_adj.as_dict()['f1']}  connectivity F1 {s_walk.as_dict()['f1']}")

    summary = {
        "dataset": "ResPlan", "split": args.split, "plans_scored": len(rows), "errors": len(errors),
        "engine_adjacency": s_adj.as_dict(),
        "engine_connectivity": s_walk.as_dict(),
        "proposed_adjacency": {**s_prop.as_dict(), "wall_mm": args.wall_mm, "min_contact_mm": args.min_contact_mm},
        "isolated_rooms": {k: v for k, v in iso.items()},
        "notes": [
            "Ground truth = ResPlan plan_to_graph + add_adjacency_edges (paper definition), fallback edges and front-door nodes removed.",
            "Connectivity ground truth = via_door + via_opening edges.",
            "Engine fed with correct ResPlan geometry; no image tracing involved.",
            "Scale estimated per plan from ResPlan's 'area' (m2) and the inner polygon.",
        ],
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    (out / f"summary_{stamp}.json").write_text(json.dumps({**summary, "error_list": errors[:50]}, indent=1))
    if rows:
        with open(out / f"per_plan_{stamp}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(json.dumps(summary, indent=1))
    print(f"saved to {out}")


if __name__ == "__main__":
    main()
