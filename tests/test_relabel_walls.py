import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import relabel_walls as r  # noqa: E402


def _rooms():
    rooms = np.zeros((40, 80), np.uint8)
    rooms[10:30, :] = 5                      # a room
    rooms[16:24, :] = r.WALL                 # annotated wall band, 8 px thick
    return rooms


def test_wall_shrinks_to_drawn_pixels_and_rest_is_ignored():
    rooms = _rooms()
    ev = np.zeros(rooms.shape, bool); ev[18:22, :] = True   # the plan draws a 4 px wall
    out, st = r.relabel_walls(rooms, ev, slack_px=3)
    assert (out[18:22, 5:75] == r.WALL).all()
    assert (out[16:18, 5:75] == r.IGNORE).all() and (out[22:24, 5:75] == r.IGNORE).all()
    assert (out[10:16, 5:75] == 5).all()                    # other classes untouched
    assert abs(st["wall_px_ratio"] - 0.5) < 0.05


def test_no_drawn_wall_means_all_ignored_not_background():
    rooms = _rooms()
    out, st = r.relabel_walls(rooms, np.zeros(rooms.shape, bool), slack_px=3)
    assert not (out == r.WALL).any()
    assert (out[16:24] == r.IGNORE).all()
    assert st["wall_px_new"] == 0


def test_drawn_wall_shifted_within_slack_is_moved_onto_the_drawing():
    rooms = _rooms()
    ev = np.zeros(rooms.shape, bool); ev[25:28, :] = True    # drawn 3 px below the band
    out, _ = r.relabel_walls(rooms, ev, slack_px=5)
    assert (out[25:28, 5:75] == r.WALL).all()
    ev2 = np.zeros(rooms.shape, bool); ev2[34:38, :] = True  # far away: not trusted
    out2, _ = r.relabel_walls(rooms, ev2, slack_px=5)
    assert not (out2[34:38] == r.WALL).any()


def test_specks_removed():
    rooms = _rooms()
    ev = np.zeros(rooms.shape, bool); ev[18:22, :] = True; ev[19, 10] = False; ev[17, 40] = True  # single stray pixel
    out, _ = r.relabel_walls(rooms, ev, slack_px=3)
    assert out[17, 40] != r.WALL


def test_slack_from_graph():
    g = {"graph_px": {"edges": [{"type": "external", "thickness_px": 20}, {"type": "internal", "thickness_px": 12},
                                 {"type": "separator", "thickness_px": 1}]}}
    assert r.slack_from_graph(g) == 4.0
    assert r.slack_from_graph({}) == 6.0
