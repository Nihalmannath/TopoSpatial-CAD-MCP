"""Junction-heatmap targets for the CubiCasa fine-tune (scripts/finetune_cubicasa.py)."""
import importlib.util
import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("finetune_heat", ROOT / "scripts" / "finetune_cubicasa.py")
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)

R, D, L, U = ft.ARM_R, ft.ARM_D, ft.ARM_L, ft.ARM_U


def write_graph(folder, nodes, edges, openings=()):
    folder.mkdir(parents=True, exist_ok=True)
    graph = {"nodes": [{"id": k, "x": x, "y": y} for k, (x, y) in nodes.items()],
             "edges": [{"source": a, "target": b, "type": t} for a, b, t in edges],
             "openings": [{"kind": "door", "ends_px": [list(a), list(b)]} for a, b in openings]}
    (folder / "graph.json").write_text(json.dumps({"graph_px": graph}), encoding="utf-8")


def test_channels_follow_the_pretrained_layout():
    # Measured with scripts/junction_probe.py on the pretrained weights, 10 Nov 2026.
    assert [ft.JUNCTION_CHANNEL[m] for m in (D, L, U, R)] == [0, 1, 2, 3]
    assert [ft.JUNCTION_CHANNEL[m] for m in (U | L, U | R, R | D, D | L)] == [4, 5, 6, 7]
    assert [ft.JUNCTION_CHANNEL[m] for m in (R | D | L, U | D | L, U | R | L, U | R | D)] == [8, 9, 10, 11]
    assert ft.JUNCTION_CHANNEL[R | D | L | U] == 12
    assert [ft.OPENING_CHANNEL[m] for m in (R, L, D, U)] == [13, 14, 15, 16]


def test_load_points_types(tmp_path):
    # y grows downwards: n1 is below n0, n2 to the right of n0.
    nodes = {"n0": (50, 50), "n1": (50, 150), "n2": (150, 50), "n3": (150, 150), "n4": (250, 50), "n5": (100, 100)}
    edges = [("n0", "n1", "external"), ("n0", "n2", "external"), ("n2", "n3", "internal"), ("n2", "n4", "external"),
             ("n1", "n3", "external"), ("n3", "n5", "separator")]
    write_graph(tmp_path, nodes, edges, openings=[((70, 50), (110, 50))])
    pts = {(round(float(x)), round(float(y))): (kind, int(mask)) for x, y, kind, mask in ft.load_points(tmp_path)}
    assert pts[(50, 50)] == (ft.PT_JUNCTION, R | D)          # corner
    assert pts[(150, 50)] == (ft.PT_JUNCTION, R | D | L)     # T junction
    assert pts[(150, 150)] == (ft.PT_JUNCTION, L | U)        # the separator arm is not a wall
    assert pts[(250, 50)][0] == ft.PT_IGNORE                 # wall end: label not trusted
    assert (100, 100) not in pts                             # separator-only node
    assert pts[(70, 50)] == (ft.PT_OPENING, R) and pts[(110, 50)] == (ft.PT_OPENING, L)
    assert ft.load_points(tmp_path / "missing") is None


def test_straight_node_is_not_a_junction(tmp_path):
    write_graph(tmp_path, {"a": (10, 10), "b": (50, 10), "c": (90, 10)}, [("a", "b", "internal"), ("b", "c", "internal")])
    pts = ft.load_points(tmp_path)
    assert not any(round(float(x)) == 50 for x, _, _, _ in pts)


def test_points_follow_the_image_through_augmentation():
    h = w = 300
    img = np.full((h, w, 3), 255, np.uint8)
    rooms = np.zeros((h, w), np.uint8)
    icons = np.zeros((h, w), np.uint8)
    pts = np.asarray([[100, 120, ft.PT_JUNCTION, R | D], [200, 180, ft.PT_JUNCTION, U | R | L]], np.float32)
    step = {R: (1, 0), D: (0, 1), L: (-1, 0), U: (0, -1)}
    checked = 0
    for seed in range(40):
        marks = np.ones((h, w), np.float32)
        for n, (x, y, _, mask) in enumerate(pts):
            marks[int(y) - 2:int(y) + 3, int(x) - 2:int(x) + 3] = 100 + n
            for bit, (dx, dy) in step.items():
                if int(mask) & bit:
                    ax, ay = int(x) + 14 * dx, int(y) + 14 * dy
                    marks[ay - 2:ay + 3, ax - 2:ax + 3] = 1000 + 16 * n + bit
        random.seed(seed)
        _, _, _, m2, p2 = ft.augment(img, rooms, icons, marks, pts, 256)
        for n in range(len(pts)):
            ys, xs = np.nonzero(m2 == 100 + n)
            if len(xs) < 12 or xs.min() == 0 or ys.min() == 0 or xs.max() == 255 or ys.max() == 255:
                continue
            px, py, _, mask = p2[n]
            assert abs(xs.mean() - px) <= 2.5 and abs(ys.mean() - py) <= 2.5
            for bit in step:
                ys2, xs2 = np.nonzero(m2 == 1000 + 16 * n + bit)
                if len(xs2) < 6:
                    continue
                vx, vy = xs2.mean() - xs.mean(), ys2.mean() - ys.mean()
                seen = (R if vx > 0 else L) if abs(vx) > abs(vy) else (D if vy > 0 else U)
                assert int(mask) & seen
                checked += 1
    assert checked > 60


def test_render_and_scores():
    pts = np.asarray([[40, 40, ft.PT_JUNCTION, R | D], [90, 40, ft.PT_OPENING, R], [40, 100, ft.PT_IGNORE, R]], np.float32)
    target, valid = ft.render_heat(pts, 128, 128, 6.0)
    assert target.shape == (ft.HEAT_CHANNELS, 128, 128)
    assert target[6, 40, 40] == pytest.approx(1.0) and target[13, 40, 90] == pytest.approx(1.0)
    assert target[6, 40, 47] == pytest.approx(np.exp(-49 / 72), abs=1e-3)
    assert target.sum(axis=(1, 2)).nonzero()[0].tolist() == [6, 13]
    assert valid[:, 100, 40].sum() == 0 and valid[:, 40, 40].all()
    assert ft.junction_scores(target, pts) == [1, 1, 1, 1, 1, 1]
    assert ft.junction_scores(np.zeros_like(target), pts)[:5] == [1, 0, 1, 0, 0]


def test_heatmap_loss():
    pts = np.asarray([[40, 40, ft.PT_JUNCTION, R | D]], np.float32)
    target, valid = (torch.from_numpy(a)[None] for a in ft.render_heat(pts, 64, 64, 6.0))
    assert ft.heatmap_loss(target.clone(), target, valid).item() == 0
    pred = torch.zeros_like(target, requires_grad=True)
    loss = ft.heatmap_loss(pred, target, valid)
    loss.backward()
    assert loss.item() > 0 and torch.count_nonzero(pred.grad) > 0
    # nothing valid (a sample without a graph): zero loss, zero gradient
    pred2 = torch.rand_like(target, requires_grad=True)
    loss2 = ft.heatmap_loss(pred2, target, torch.zeros_like(valid))
    loss2.backward()
    assert loss2.item() == 0 and torch.count_nonzero(pred2.grad) == 0
