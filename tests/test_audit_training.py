import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_training as a  # noqa: E402


def test_iou_and_nan():
    g = np.zeros((10, 10), bool); g[2:6, 2:6] = True
    p = np.zeros((10, 10), bool); p[2:6, 4:8] = True
    assert abs(a.iou(g, p) - 8 / 24) < 1e-9
    assert a.iou(np.zeros((3, 3), bool), np.zeros((3, 3), bool)) != a.iou(np.zeros((3, 3), bool), np.zeros((3, 3), bool))


def test_iou_respects_valid_mask():
    g = np.zeros((4, 4), bool); g[:, :2] = True
    p = np.zeros((4, 4), bool); p[:, 1:3] = True
    valid = np.ones((4, 4), bool); valid[:, 2:] = False
    assert a.iou(g, p, valid) == 4 / 8


def test_diff_panel_colours():
    img = np.full((4, 4, 3), 255, np.uint8)
    g = np.zeros((4, 4), bool); g[0, 0] = g[0, 1] = True
    p = np.zeros((4, 4), bool); p[0, 1] = p[0, 2] = True
    d = a.diff_panel(img, g, p)
    assert tuple(d[0, 0]) == a.DIFF_COLORS["missed"]
    assert tuple(d[0, 1]) == a.DIFF_COLORS["agree"]
    assert tuple(d[0, 2]) == a.DIFF_COLORS["extra"]


def test_rank_regressions_worst_first_nan_last():
    rows = {"a": {"ft_wall_iou": 0.5, "pre_wall_iou": 0.6},
            "b": {"ft_wall_iou": 0.2, "pre_wall_iou": 0.7},
            "c": {"ft_wall_iou": float("nan"), "pre_wall_iou": 0.7},
            "d": {"ft_wall_iou": 0.9, "pre_wall_iou": 0.5}}
    assert a.rank_regressions(rows) == ["b", "a", "d", "c"]


def test_annotation_stats_thickness():
    wall = np.zeros((60, 60), bool); wall[10:20, :] = True  # 10 px thick horizontal band
    stats = a.annotation_stats(wall, np.ones_like(wall), px_per_mm=0.5)
    assert abs(stats["wall_thickness_mm"] - 20.0) < 1e-6
    assert stats["ignored_share"] == 0.0
