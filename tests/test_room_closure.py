"""Room closure from the raw network masks (src/trace_engine/room_closure.py)."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("scipy")
from trace_engine import neural, room_closure, rooms  # noqa: E402
from trace_engine.vision import Run  # noqa: E402

MM_PER_PX = 10.0   # 1 px = 10 mm
T = 20.0           # wall thickness, px (200 mm)


def two_room_masks():
    """A 6 x 4 m box with a partition at x = 300 px. Returns (room map, wall mask)."""
    walls = np.zeros((500, 700), np.uint8)
    walls[40:60, 40:660] = 1     # top      (centre y = 50)
    walls[440:460, 40:660] = 1   # bottom   (centre y = 450)
    walls[40:460, 40:60] = 1     # left     (centre x = 50)
    walls[40:460, 640:660] = 1   # right    (centre x = 650)
    walls[40:460, 290:310] = 1   # partition (centre x = 300)
    room_map = np.where(walls > 0, 2, 0).astype(np.uint8)
    room_map[60:440, 60:290] = 5   # Bed Room
    room_map[60:440, 310:640] = 4  # Living Room
    return room_map, walls


def outer_runs():
    return [Run("h", 50.0, 50.0, 650.0, T), Run("h", 450.0, 50.0, 650.0, T),
            Run("v", 50.0, 50.0, 450.0, T), Run("v", 650.0, 50.0, 450.0, T)]


def test_missing_partition_is_closed_with_a_separator():
    room_map, walls = two_room_masks()
    res = room_closure.close_rooms(room_map, walls, outer_runs(), [], MM_PER_PX, thickness=T)
    assert res.closed >= 1 and res.separators
    # one vertical divider on the partition centreline, from the top wall to the bottom wall
    (a, b, _), = [s for s in res.separators if abs(s[0][0] - s[1][0]) < 1]
    assert abs(a[0] - 300.0) <= 3 and abs(b[0] - 300.0) <= 3
    assert sorted((a[1], b[1])) == pytest.approx([50.0, 450.0], abs=1.0)
    # every other side already has a wall: no duplicates
    assert all(abs(s[0][0] - s[1][0]) < 1 for s in res.separators)


def test_nothing_is_added_when_the_walls_already_close_the_rooms():
    room_map, walls = two_room_masks()
    runs = outer_runs() + [Run("v", 300.0, 50.0, 450.0, T)]
    res = room_closure.close_rooms(room_map, walls, runs, [], MM_PER_PX, thickness=T)
    assert res.separators == [] and res.closed == 0


def test_only_the_missing_stretch_of_a_partition_is_added():
    room_map, walls = two_room_masks()
    runs = outer_runs() + [Run("v", 300.0, 50.0, 330.0, T)]   # partition traced for 70 % of its length
    res = room_closure.close_rooms(room_map, walls, runs, [], MM_PER_PX, thickness=T)
    assert len(res.separators) == 1
    a, b, _ = res.separators[0]
    assert a[0] == pytest.approx(300.0) and b[0] == pytest.approx(300.0)   # on the traced wall's centreline
    assert min(a[1], b[1]) == pytest.approx(340.0, abs=1.0) and max(a[1], b[1]) == pytest.approx(450.0, abs=1.0)


def test_existing_separator_counts_as_a_boundary():
    room_map, walls = two_room_masks()
    seps = [((300.0, 50.0), (300.0, 450.0), 1.0)]
    res = room_closure.close_rooms(room_map, walls, outer_runs(), seps, MM_PER_PX, thickness=T)
    assert res.separators == []


def test_open_space_outside_the_masks_is_left_alone():
    room_map, walls = two_room_masks()
    walls[:] = 0
    walls[40:60, 40:660] = 1  # a single wall: the masks enclose nothing
    res = room_closure.close_rooms(room_map, walls, [Run("h", 50.0, 50.0, 650.0, T)], [], MM_PER_PX, thickness=T)
    assert res.separators == []


def test_no_inputs_no_output():
    room_map, walls = two_room_masks()
    assert room_closure.close_rooms(None, walls, outer_runs(), [], MM_PER_PX).separators == []
    assert room_closure.close_rooms(room_map, walls, [], [], MM_PER_PX).separators == []
    assert room_closure.close_rooms(room_map, walls, outer_runs(), [], 0.0).separators == []


def test_profile_switch(tmp_path):
    w = tmp_path / "w.pth"
    w.write_bytes(b"x")
    other = tmp_path / "other.pth"
    other.write_bytes(b"y")
    on = {"weights": str(w), "wall_logit_bias": 0.0, "close_rooms": True}
    assert neural.default_close_rooms(w, on) is True
    assert neural.default_close_rooms(other, on) is False            # only the promoted weights
    assert neural.default_close_rooms(w, {"weights": str(w)}) is False
    assert neural.default_close_rooms(None, on) is False


def test_inset_face_survives_a_zero_length_edge():
    square = [(0.0, 0.0), (4000.0, 0.0), (4000.0, 0.0), (4000.0, 3000.0), (0.0, 3000.0)]
    clear = rooms.inset_face(square, [100.0] * 5)
    assert len(clear) == 4
    xs, ys = [p[0] for p in clear], [p[1] for p in clear]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((100.0, 3900.0, 100.0, 2900.0))
    assert rooms.inset_face([(0.0, 0.0), (0.0, 0.0), (1.0, 0.0)], [1.0, 1.0, 1.0]) == []
