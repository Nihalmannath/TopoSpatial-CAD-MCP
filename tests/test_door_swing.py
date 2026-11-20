"""Door swing detection: arc + leaf fitted beside a wall gap."""

from __future__ import annotations

import numpy as np
import pytest

from trace_engine._cv import cv2
from trace_engine.vision import Opening, _detect_swing

T = 14  # wall thickness, px
C = 150  # wall centre line (horizontal wall)
LO, HI = 100, 170  # the gap


def _wall_with_gap() -> np.ndarray:
    img = np.zeros((300, 300), np.uint8)
    img[C - T // 2:C + T // 2 + 1, 20:LO] = 1
    img[C - T // 2:C + T // 2 + 1, HI:280] = 1
    return img


def _draw_door(img, hinge_x, radius, side):
    tip = (hinge_x, C + side * radius)
    cv2.line(img, (hinge_x, C), tip, 1, 2)
    start, end = (0, 90) if side > 0 else (270, 360)
    cv2.ellipse(img, (hinge_x, C), (radius, radius), 0, start, end, 1, 1)


@pytest.mark.parametrize("offset,scale", [(0, 1.0), (5, 1.0), (0, 0.92), (-4, 1.08)])
def test_drawn_swing_is_found_even_when_gap_is_off(offset, scale):
    img = _wall_with_gap()
    _draw_door(img, LO + offset, int(round((HI - LO) * scale)), side=1)
    op = Opening("door", "h", float(C), float(LO), float(HI))
    _detect_swing(img, op, float(T))
    assert op.swing_detected and op.hinge == "start"
    assert op.swing == "right"  # image side +1 on an 'h' wall is the right-hand side in y-up CAD


def test_hinge_at_end_and_other_side():
    img = _wall_with_gap()
    cv2.line(img, (HI, C), (HI, C - (HI - LO)), 1, 2)
    cv2.ellipse(img, (HI, C), (HI - LO, HI - LO), 0, 180, 270, 1, 1)
    op = Opening("door", "h", float(C), float(LO), float(HI))
    _detect_swing(img, op, float(T))
    assert op.swing_detected and op.hinge == "end" and op.swing == "left"


def test_bare_gap_has_no_swing():
    op = Opening("door", "h", float(C), float(LO), float(HI))
    _detect_swing(_wall_with_gap(), op, float(T))
    assert not op.swing_detected


def test_arc_without_leaf_is_not_a_door():
    img = _wall_with_gap()
    cv2.ellipse(img, (LO, C), (HI - LO, HI - LO), 0, 0, 90, 1, 1)
    op = Opening("door", "h", float(C), float(LO), float(HI))
    _detect_swing(img, op, float(T))
    assert not op.swing_detected
