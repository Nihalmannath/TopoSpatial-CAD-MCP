"""Close rooms that the network sees and the traced walls do not.

The wall tracer only makes a room when its straight wall runs surround it. On
brochure plans it loses rooms because a piece of the boundary goes missing on
the way from the wall mask to wall runs: the room then merges with its
neighbour or stays open. The raw network masks (wall, door, window, railing)
still enclose that room.

``close_rooms`` finds rooms enclosed by the raw masks, compares each with the
face the traced walls give at the same place, and where the traced face is
open or much larger it adds ``separator`` edges (they bound rooms and are
never built as walls) on the stretches of the room's bounding rectangle that
have no traced wall.

Measured on the 36 validation plans with model J (10 Nov 2026, canonical):
rooms found 206 -> 244 of 371 with 349 predicted (precision 0.67 -> 0.70,
recall 0.56 -> 0.66, mean IoU 0.45 -> 0.53, room outline error 167 -> 143 mm);
walls, doors and windows unchanged; no plan lost a room. Rooms enclosed by the
raw masks matched a labelled room about 9 times out of 10.

Two further steps exist but are off by default because they were not precise
enough on those plans:

* ``add_open`` - close compact, rectangular room-class regions (Outdoor, ...)
  that the walls leave open: +3 rooms for +16 predictions on top of the above
  (most extras were "balcony below" boxes that are not rooms of the floor);
* ``split`` - divide a closed face that holds two large regions of different
  classes: only 4 of 15 dividers were right (Bed Room / Living Room confusion).

Everything is axis-aligned, like the wall runs. Thresholds are millimetres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

Point = Tuple[float, float]
Segment = Tuple[Point, Point, float]

# CubiCasa room classes that are not rooms.
NOT_ROOM = (0, 2, 8)  # Background, Wall, Railing
RAILING = 8


@dataclass
class RoomClosureConfig:
    close_masks: bool = True         # close rooms that the raw masks enclose
    add_open: bool = False           # close compact class regions the walls leave open (see below)
    split: bool = False              # divide faces by class (unreliable so far)
    min_room_mm2: float = 1.0e6      # smallest region that can become a room (1 m2)
    split_share: float = 0.20        # each part of a split holds at least this share of the face
    split_purity: float = 0.75       # share of each part that must end up on its own side of the divider
    min_divider_mm: float = 600.0    # shorter class boundaries are noise
    min_rect: float = 0.70           # region area / bounding box area (rooms are rectangles)
    min_contact: float = 0.50        # share of the outline that touches walls / railings
    min_free: float = 0.70           # share of the region that the walls leave open
    bigger: float = 1.5              # a traced face this many times larger than the mask room has merged rooms
    mask_gap_mm: float = 80.0        # mask gaps up to twice this are treated as closed
    min_piece_mm: float = 200.0      # shortest divider piece worth adding
    gap_mm: float = 150.0            # wall gaps up to twice this are treated as closed
    snap_mm: float = 300.0           # a divider this close to a parallel wall is that wall
    clean_mm: float = 60.0           # speckle removal in the class map


@dataclass
class RoomClosureResult:
    separators: List[Segment] = field(default_factory=list)
    splits: int = 0
    added: int = 0
    closed: int = 0
    notes: List[Dict[str, Any]] = field(default_factory=list)


def _components(room_map: np.ndarray, min_px: float, clean_px: int):
    """Label map of class regions (one label per connected region of one class)."""
    from scipy import ndimage

    lab = np.zeros(room_map.shape, np.int32)
    classes = [0]
    for c in np.unique(room_map):
        if int(c) in NOT_ROOM:
            continue
        m = room_map == c
        if clean_px > 0:
            m = ndimage.binary_opening(m, iterations=clean_px)
        part, k = ndimage.label(m)
        if not k:
            continue
        sizes = np.bincount(part.ravel(), minlength=k + 1)
        for q in np.nonzero(sizes[1:] >= min_px)[0] + 1:
            lab[part == q] = len(classes)
            classes.append(int(c))
    return lab, classes


def _barrier(shape, runs, separators) -> np.ndarray:
    """Traced walls (full thickness) and existing separators as a mask."""
    h, w = shape
    out = np.zeros(shape, bool)
    for r in runs:
        half = max(1.0, r.t / 2.0)
        if r.axis == "h":
            y0, y1 = int(round(r.c - half)), int(round(r.c + half)) + 1
            x0, x1 = int(round(r.lo - half)), int(round(r.hi + half)) + 1
        else:
            x0, x1 = int(round(r.c - half)), int(round(r.c + half)) + 1
            y0, y1 = int(round(r.lo - half)), int(round(r.hi + half)) + 1
        out[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    for a, b, _ in separators:
        x0, x1 = sorted((int(round(a[0])), int(round(b[0]))))
        y0, y1 = sorted((int(round(a[1])), int(round(b[1]))))
        if x1 - x0 >= y1 - y0:  # horizontal (or a short skew): one row band
            yc = int(round((a[1] + b[1]) / 2.0))
            out[max(0, yc - 1):min(h, yc + 2), max(0, x0):min(w, x1 + 1)] = True
        else:
            xc = int(round((a[0] + b[0]) / 2.0))
            out[max(0, y0):min(h, y1 + 1), max(0, xc - 1):min(w, xc + 2)] = True
    return out


def _anchor(runs, axis: str, c: float, end: float, reach: float) -> float:
    """Move the end of a divider (running along ``axis`` at coordinate ``c``)
    onto the centreline of the perpendicular wall it stops at."""
    best, best_d = end, reach
    for r in runs:
        if r.axis == axis:
            continue
        if r.lo - r.t <= c <= r.hi + r.t:
            d = abs(r.c - end) - r.t / 2.0
            if d <= best_d:
                best, best_d = r.c, d
    return best


@dataclass
class _Line:
    axis: str
    c: float
    lo: float
    hi: float
    t: float


def _lines(runs, separators) -> List[_Line]:
    """Wall runs plus the existing separators, all as axis-aligned lines."""
    out = [_Line(r.axis, float(r.c), float(r.lo), float(r.hi), float(r.t)) for r in runs]
    for a, b, _ in separators:
        if abs(a[0] - b[0]) >= abs(a[1] - b[1]):
            out.append(_Line("h", (a[1] + b[1]) / 2.0, min(a[0], b[0]), max(a[0], b[0]), 1.0))
        else:
            out.append(_Line("v", (a[0] + b[0]) / 2.0, min(a[1], b[1]), max(a[1], b[1]), 1.0))
    return out


def _side(lines: Sequence[_Line], axis: str, c: float, lo: float, hi: float, snap: float):
    """How much of one rectangle side already has a line along it.

    Returns (coordinate to use, covered intervals). The coordinate is the
    centreline of the line that covers most of the side, or ``c`` when none does."""
    best_c, best_len, covered = c, 0.0, []
    for ln in lines:
        if ln.axis != axis or abs(ln.c - c) > ln.t / 2.0 + snap:
            continue
        a, b = max(ln.lo - ln.t / 2.0, lo), min(ln.hi + ln.t / 2.0, hi)
        if b - a <= 0:
            continue
        covered.append((a, b))
        if b - a > best_len:
            best_c, best_len = ln.c, b - a
    covered.sort()
    merged: List[List[float]] = []
    for a, b in covered:
        if merged and a <= merged[-1][1] + 1.0:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return best_c, [(a, b) for a, b in merged]


def _seg(axis: str, c: float, lo: float, hi: float) -> Segment:
    return (((lo, c), (hi, c), 1.0) if axis == "h" else ((c, lo), (c, hi), 1.0))


def _close_rectangle(lines: List[_Line], box, snap: float, min_piece: float, min_cover: float = 0.6):
    """Dividers that close the rectangle ``box`` = (x0, y0, x1, y1).

    A side with a line along most of it takes that line's coordinate; the
    uncovered stretches of every side become dividers. Returns (segments,
    final box, names of the sides that had no line)."""
    x0, y0, x1, y1 = box
    spec = {"top": ("h", y0, x0, x1), "bottom": ("h", y1, x0, x1), "left": ("v", x0, y0, y1), "right": ("v", x1, y0, y1)}
    found = {k: _side(lines, a, c, lo, hi, snap) for k, (a, c, lo, hi) in spec.items()}
    walled = {k: sum(b - a for a, b in found[k][1]) >= min_cover * max(spec[k][3] - spec[k][2], 1.0) for k in spec}
    cx0 = found["left"][0] if walled["left"] else x0
    cx1 = found["right"][0] if walled["right"] else x1
    cy0 = found["top"][0] if walled["top"] else y0
    cy1 = found["bottom"][0] if walled["bottom"] else y1
    final = {"top": ("h", cy0, cx0, cx1), "bottom": ("h", cy1, cx0, cx1), "left": ("v", cx0, cy0, cy1), "right": ("v", cx1, cy0, cy1)}
    segs: List[Segment] = []
    for k, (axis, c, lo, hi) in final.items():
        if not walled[k]:
            if hi - lo >= min_piece:
                segs.append(_seg(axis, c, lo, hi))
            continue
        # walled side: add only the stretches the line leaves open
        cur = lo
        for a, b in found[k][1] + [(hi, hi)]:
            if a - cur >= min_piece:
                segs.append(_seg(axis, c, cur, a))
            cur = max(cur, b)
    return segs, (cx0, cy0, cx1, cy1), [k for k in spec if not walled[k]]


def close_rooms(
    room_map: Optional[np.ndarray],
    wall_mask: Optional[np.ndarray],
    runs: Sequence[Any],
    separators: Sequence[Segment],
    mm_per_px: float,
    config: Optional[RoomClosureConfig] = None,
    window_mask: Optional[np.ndarray] = None,
    door_mask: Optional[np.ndarray] = None,
    thickness: float = 0.0,
) -> RoomClosureResult:
    """Dividers (separator segments, image px) that close rooms the network sees
    and the traced walls do not. ``thickness`` is the wall thickness in px."""
    from scipy import ndimage

    cfg = config or RoomClosureConfig()
    res = RoomClosureResult()
    if room_map is None or not len(runs) or not (mm_per_px and mm_per_px > 0):
        return res
    room_map = np.asarray(room_map)
    h, w = room_map.shape
    px = 1.0 / mm_per_px  # px per mm
    min_px = cfg.min_room_mm2 * px * px
    gap = max(1, int(round(cfg.gap_mm * px)))
    snap = cfg.snap_mm * px
    min_piece = cfg.min_piece_mm * px
    seps: List[Segment] = list(separators)

    def traced_faces():
        closed = ndimage.binary_dilation(_barrier((h, w), runs, seps), iterations=gap)
        faces, n = ndimage.label(~closed)
        edge = np.unique(np.concatenate([faces[0], faces[-1], faces[:, 0], faces[:, -1]]))
        outside = np.zeros(n + 1, bool)
        outside[edge] = True
        outside[0] = False
        return faces, n, outside, np.bincount(faces.ravel(), minlength=n + 1)

    # ---- 1. rooms the raw masks enclose but the traced walls do not ---------------
    if cfg.close_masks and wall_mask is not None:
        faces, n_faces, outside, face_area = traced_faces()
        solid = (np.asarray(wall_mask) > 0) | (room_map == RAILING)
        for m in (window_mask, door_mask):
            if m is not None:
                solid |= np.asarray(m) > 0
        mgap = max(1, int(round(cfg.mask_gap_mm * px)))
        mfaces, n_m = ndimage.label(~ndimage.binary_dilation(solid, iterations=mgap))
        m_edge = set(np.unique(np.concatenate([mfaces[0], mfaces[-1], mfaces[:, 0], mfaces[:, -1]])).tolist())
        m_area = np.bincount(mfaces.ravel(), minlength=n_m + 1)
        grow = mgap + thickness / 2.0  # from the shrunken mask room out to the wall centreline
        order = sorted((f for f in range(1, n_m + 1) if f not in m_edge and m_area[f] >= min_px), key=lambda f: m_area[f])
        m_slices = ndimage.find_objects(mfaces)
        for f in order:
            sl = m_slices[f - 1]
            room = mfaces[sl] == f
            if m_area[f] / float(room.size) < cfg.min_rect:
                continue
            under = np.bincount(faces[sl][room], minlength=n_faces + 1)
            under[0] = 0
            if not under.any():
                continue
            t = int(under.argmax())
            # a traced face of about the same size means the room is already closed
            if not outside[t] and face_area[t] < cfg.bigger * m_area[f]:
                continue
            box = (sl[1].start - grow, sl[0].start - grow, sl[1].stop - 1 + grow, sl[0].stop - 1 + grow)
            new, final, open_sides = _close_rectangle(_lines(runs, seps), box, snap, min_piece)
            if not new:
                continue
            seps.extend(new)
            res.separators.extend(new)
            res.closed += 1
            res.notes.append({"kind": "close", "box": [round(float(v), 1) for v in final], "open_sides": open_sides,
                              "pieces": len(new), "area_m2": round(m_area[f] * mm_per_px * mm_per_px / 1e6, 2),
                              "traced_face": "open" if outside[t] else round(float(face_area[t] / m_area[f]), 2)})

    comp = classes = None
    if cfg.add_open or cfg.split:
        comp, classes = _components(room_map, min_px, max(0, int(round(cfg.clean_mm * px))))
        if len(classes) <= 1:
            return res

    # ---- 2. compact class regions the walls leave open ---------------------------
    if cfg.add_open:
        faces, n_faces, outside, face_area = traced_faces()
        out_mask = outside[faces]
        solid = _barrier((h, w), runs, seps)
        if wall_mask is not None:
            solid |= np.asarray(wall_mask) > 0
        solid |= room_map == RAILING
        near_solid = ndimage.binary_dilation(solid, iterations=max(2, int(round(250.0 * px))))
        comp_slices = ndimage.find_objects(comp)
        for q in range(1, len(classes)):
            sl = comp_slices[q - 1]
            if sl is None:
                continue
            region = comp[sl] == q
            free = region & out_mask[sl]
            n_free = int(free.sum())
            if n_free < min_px or n_free < cfg.min_free * region.sum():
                continue
            ys, xs = np.nonzero(free)
            x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
            rect = n_free / float((x1 - x0 + 1) * (y1 - y0 + 1))
            if rect < cfg.min_rect:
                continue
            rim = free & ~ndimage.binary_erosion(free)
            contact = float(near_solid[sl][rim].mean())
            if contact < cfg.min_contact:
                continue
            box = (x0 + sl[1].start - gap, y0 + sl[0].start - gap, x1 + sl[1].start + gap, y1 + sl[0].start + gap)
            new, final, open_sides = _close_rectangle(_lines(runs, seps), box, snap, min_piece)
            if not new or len(open_sides) == 4:
                # nothing to add, or no side has a wall: not attached to the building
                continue
            seps.extend(new)
            res.separators.extend(new)
            res.added += 1
            res.notes.append({"kind": "add", "class": classes[q], "box": [round(float(v), 1) for v in final],
                              "rect": round(float(rect), 3), "contact": round(contact, 3),
                              "area_m2": round(n_free * mm_per_px * mm_per_px / 1e6, 2),
                              "free": round(n_free / float(region.sum()), 3), "open_sides": open_sides})

    # ---- 3. (optional) split closed faces that hold two rooms ----------------------
    if cfg.split:
        res.separators.extend(_split_faces(room_map, comp, classes, runs, seps, mm_per_px, cfg, res))
    return res


def _split_faces(room_map, comp, classes, runs, seps, mm_per_px, cfg, res) -> List[Segment]:
    from scipy import ndimage

    h, w = room_map.shape
    px = 1.0 / mm_per_px
    min_px = cfg.min_room_mm2 * px * px
    gap = max(1, int(round(cfg.gap_mm * px)))
    snap = cfg.snap_mm * px
    lines = _lines(runs, seps)
    closed = ndimage.binary_dilation(_barrier((h, w), runs, seps), iterations=gap)
    faces, n_faces = ndimage.label(~closed)
    edge = set(np.unique(np.concatenate([faces[0], faces[-1], faces[:, 0], faces[:, -1]])).tolist())
    face_area = np.bincount(faces.ravel(), minlength=n_faces + 1)
    slices = ndimage.find_objects(faces)
    out: List[Segment] = []
    for f in range(1, n_faces + 1):
        if f in edge or face_area[f] < 2 * min_px or slices[f - 1] is None:
            continue
        sl = slices[f - 1]
        inside = faces[sl] == f
        local = comp[sl]
        counts = np.bincount(local[inside], minlength=len(classes))
        counts[0] = 0
        parts = [int(q) for q in np.nonzero(counts >= max(min_px, cfg.split_share * face_area[f]))[0]]
        if len(parts) < 2:
            continue
        seeds = np.zeros(inside.shape, np.int32)
        for q in parts:
            seeds[inside & (local == q)] = q
        idx = ndimage.distance_transform_edt(seeds == 0, return_distances=False, return_indices=True)
        near = np.where(inside, seeds[idx[0], idx[1]], 0)
        y_off, x_off = sl[0].start, sl[1].start
        for i, qa in enumerate(parts):
            for qb in parts[i + 1:]:
                if classes[qa] == classes[qb]:
                    continue
                bx = (near[:, :-1] != near[:, 1:]) & np.isin(near[:, :-1], (qa, qb)) & np.isin(near[:, 1:], (qa, qb))
                by = (near[:-1, :] != near[1:, :]) & np.isin(near[:-1, :], (qa, qb)) & np.isin(near[1:, :], (qa, qb))
                ys = np.concatenate([np.nonzero(bx)[0], np.nonzero(by)[0]])
                xs = np.concatenate([np.nonzero(bx)[1], np.nonzero(by)[1]])
                if len(xs) < cfg.min_divider_mm * px:
                    continue
                axis = "v" if np.ptp(np.percentile(xs, [5, 95])) < np.ptp(np.percentile(ys, [5, 95])) else "h"
                A, B = near == qa, near == qb
                if axis == "v":
                    c = float(np.median(xs))
                    grid = np.arange(near.shape[1])[None, :]
                    line = inside[:, min(int(round(c)), inside.shape[1] - 1)]
                    mid = int(np.median(ys))
                else:
                    c = float(np.median(ys))
                    grid = np.arange(near.shape[0])[:, None]
                    line = inside[min(int(round(c)), inside.shape[0] - 1), :]
                    mid = int(np.median(xs))
                a_side = (A & (grid <= c)).sum() / max(A.sum(), 1)
                b_side = (B & (grid > c)).sum() / max(B.sum(), 1)
                purity = min(max(a_side, 1 - a_side), max(b_side, 1 - b_side))
                opposite = (a_side >= 0.5) == (b_side >= 0.5)  # A below the line and B above it, or the reverse
                if purity < cfg.split_purity or not opposite:
                    continue
                hits = np.nonzero(line)[0]
                if not len(hits):
                    continue
                mid = int(hits[np.argmin(np.abs(hits - mid))])
                lo = hi = mid
                while lo > 0 and line[lo - 1]:
                    lo -= 1
                while hi < len(line) - 1 and line[hi + 1]:
                    hi += 1
                if hi - lo < cfg.min_divider_mm * px:
                    continue
                off_c, off_l = (x_off, y_off) if axis == "v" else (y_off, x_off)
                c_img, lo_img, hi_img = c + off_c, lo + off_l, hi + off_l
                _, covered = _side(lines, axis, c_img, lo_img, hi_img, snap)
                if sum(b - a for a, b in covered) >= 0.6 * (hi_img - lo_img):
                    continue  # the class boundary is a wall that is already traced
                lo_img = _anchor(runs, axis, c_img, lo_img - gap, gap + snap)
                hi_img = _anchor(runs, axis, c_img, hi_img + gap, gap + snap)
                out.append(_seg(axis, c_img, lo_img, hi_img))
                res.splits += 1
                res.notes.append({"kind": "split", "classes": [classes[qa], classes[qb]], "axis": axis,
                                  "c": round(c_img, 1), "lo": round(lo_img, 1), "hi": round(hi_img, 1)})
    return out
