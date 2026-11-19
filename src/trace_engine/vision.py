"""Raster floor-plan recogniser: image -> ``TraceResult``.

Designed for drafted residential plans (solid or hatched walls, orthogonal
layout) such as brochure plans and CAD exports. Steps:

1. **Wall mask** - dark pixels opened with a square kernel just under the
   wall thickness, which removes text, dimension lines, furniture, glazing
   and stair treads but keeps walls. Thickness is the dominant dark run
   length, measured, not assumed.
2. **Plan region** - the largest cluster of wall pixels (drops title bars,
   footers and icon strips).
3. **Rectilinear runs** - horizontal and vertical wall bands from scanline
   runs, each giving a centerline, extent and thickness.
4. **Graph** - run ends snapped onto the centerline of the perpendicular wall
   they meet (L/T junctions).
5. **Openings** - gaps between collinear runs, or between a run end and a
   perpendicular wall, are bridged into one wall plus a hosted opening. A gap
   whose band contains thin glazing lines is a window, otherwise a door; a
   quarter-circle swing next to the gap, if drawn, sets hinge and swing side.
6. **Labels and scale** - Tesseract OCR (optional) reads room names and
   "W x D" sizes. The scale (mm per pixel) is fitted from those sizes against
   the traced clear rooms; without them it falls back to a nominal wall
   thickness.

Needs NumPy plus OpenCV, or SciPy + Pillow as a fallback; pytesseract is used
when available.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ._cv import cv2

from .graph import clean
from .models import TraceResult, WallsGraph
from .rooms import point_in_polygon, recover_rooms

Point = Tuple[float, float]


@dataclass
class Run:
    """A straight wall band. ``axis`` is 'h' (constant y) or 'v'."""

    axis: str
    c: float          # centerline coordinate (y for 'h', x for 'v')
    lo: float         # extent start along the axis (x for 'h', y for 'v')
    hi: float
    t: float          # thickness in px


@dataclass
class Opening:
    kind: str         # "door" | "window"
    axis: str
    c: float
    lo: float
    hi: float
    hinge: str = "start"
    swing: str = "left"
    swing_detected: bool = False


@dataclass
class Label:
    text: str
    box: Tuple[int, int, int, int]  # x, y, w, h
    size_cm: Optional[Tuple[float, float]] = None

    @property
    def center(self) -> Point:
        x, y, w, h = self.box
        return (x + w / 2.0, y + h / 2.0)


@dataclass
class VisionReport:
    image_size: Tuple[int, int]
    plan_bbox: Tuple[int, int, int, int]
    wall_thickness_px: float
    mm_per_px: float
    scale_method: str
    runs: List[Run] = field(default_factory=list)
    openings: List[Opening] = field(default_factory=list)
    labels: List[Label] = field(default_factory=list)
    scale_samples: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    separators: List[Any] = field(default_factory=list)
    engine: str = "classic"


# ------------------------------------------------------------- 1. wall mask
def _dark_mask(gray: np.ndarray) -> np.ndarray:
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    threshold = min(float(threshold), 140.0)
    return (gray < threshold).astype(np.uint8)


def _run_lengths(mask: np.ndarray) -> np.ndarray:
    out = []
    for m in (mask, mask.T):
        padded = np.pad(m, ((0, 0), (1, 1)))
        d = np.diff(padded.astype(np.int8), axis=1)
        starts = np.argwhere(d == 1)
        ends = np.argwhere(d == -1)
        out.append(ends[:, 1] - starts[:, 1])
    return np.concatenate(out)


def estimate_wall_thickness(dark: np.ndarray, min_px: int = 6, max_px: int = 80) -> float:
    runs = _run_lengths(dark)
    runs = runs[(runs >= min_px) & (runs <= max_px)]
    if runs.size == 0:
        raise ValueError("no wall-like dark bands found; is this a floor plan?")
    counts = np.bincount(runs)
    # Weight by length so long-thin text strokes don't win over walls.
    peak = int(np.argmax(counts * np.arange(len(counts)) ** 0.5))
    lo, hi = max(min_px, peak - 2), peak + 3
    window = counts[lo:hi]
    return float((np.arange(lo, lo + len(window)) * window).sum() / window.sum())


def band_width_median(mask: np.ndarray, line_px: int = 60, min_px: int = 4, max_px: int = 80) -> Optional[float]:
    """Median width across long straight stretches of ``mask`` (px), or None if there are none.

    Only pixels inside a horizontal or vertical stretch at least ``line_px`` long count, and the
    width is measured across that stretch. Speckle, ragged edges and corner blocks don't form
    such stretches, so they cannot drag the estimate the way they drag the run-length histogram."""
    mask = (np.asarray(mask) > 0).astype(np.uint8)
    h = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, line_px), np.uint8))
    v = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((line_px, 1), np.uint8))
    # across horizontal stretches = run lengths down the columns; across vertical ones = along the rows
    widths = np.concatenate([_row_runs(h.T), _row_runs(v)])
    widths = widths[(widths >= min_px) & (widths <= max_px)]
    return float(np.median(widths)) if widths.size else None


def _row_runs(a: np.ndarray) -> np.ndarray:
    """Lengths of the 1-runs along each row of ``a``."""
    d = np.diff(np.pad(a, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    return np.argwhere(d == -1)[:, 1] - np.argwhere(d == 1)[:, 1]


# The run-length estimate is replaced by the band median when it is this many times larger.
THICKNESS_GUARD_RATIO = 1.3


def mask_wall_thickness(mask: np.ndarray, min_px: int = 4) -> Tuple[float, str]:
    """Wall thickness of a network wall mask (px) and how it was obtained.

    The run-length estimate is kept unless it exceeds the band median by THICKNESS_GUARD_RATIO.
    A ragged or speckled mask (fine-tuned checkpoint on 6_SIXTH_15/1jca, 3 Nov 2026) has no clear
    run-length peak; the estimate jumped to 48 px against ~21 px walls, and every threshold scaled
    from it (opening kernel 24 px, run length 77 px) erased the interior walls: 183 -> 5 walls."""
    try:
        t = estimate_wall_thickness(mask, min_px=min_px)
    except ValueError:
        t = None
    band = band_width_median(mask, min_px=min_px)
    if t is None:
        return (band, "band median (no run-length peak)") if band else (10.0, "default 10 px")
    if band and t > THICKNESS_GUARD_RATIO * band:
        return band, f"band median {band:.1f} px (run-length estimate {t:.1f} px rejected)"
    return t, "run-length peak"


def drop_frame(mask: np.ndarray) -> np.ndarray:
    """Remove dark regions touching the image border (brochure banners,
    page frames, colour tabs) so they cannot pose as walls."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    h, w = mask.shape
    out = mask.copy()
    for i in range(1, n):
        x, y, bw, bh, _ = stats[i]
        if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
            out[labels == i] = 0
    return out


def wall_mask(gray: np.ndarray, thickness: float) -> np.ndarray:
    dark = _dark_mask(gray)
    k = max(3, int(round(thickness * 0.6)))
    opened = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    return opened


def plan_region(walls: np.ndarray, thickness: float) -> Tuple[int, int, int, int]:
    d = max(3, int(thickness * 3))
    grown = cv2.dilate(walls, np.ones((d, d), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(grown)
    best, best_score = None, -1
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if min(w, h) < thickness * 6:
            continue  # bars, banners, single wall stubs
        px = int(walls[y:y + h, x:x + w][labels[y:y + h, x:x + w] == i].sum())
        fill = px / float(w * h)
        score = px * (1.0 - fill)  # solid blocks (banners, logos) score ~0
        if score > best_score:
            best, best_score = (int(x), int(y), int(w), int(h)), score
    if best is None:
        raise ValueError("no walls found")
    # Walls broken by openings form several clusters: grow the region with
    # nearby clusters that are not solid blocks, until nothing else joins.
    x0, y0, w, h = best
    x1, y1 = x0 + w, y0 + h
    margin = thickness * 8
    members = set()
    changed = True
    while changed:
        changed = False
        for i in range(1, n):
            if i in members:
                continue
            x, y, w, h, area = stats[i]
            px = int(walls[y:y + h, x:x + w][labels[y:y + h, x:x + w] == i].sum())
            if px == 0 or px / float(w * h) > 0.6 and max(w, h) > thickness * 10:
                continue
            if x > x1 + margin or x + w < x0 - margin or y > y1 + margin or y + h < y0 - margin:
                continue
            members.add(i)
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x + w), max(y1, y + h)
            changed = True
    pad = d // 2
    return (int(x0 + pad), int(y0 + pad), int(x1 - x0 - 2 * pad), int(y1 - y0 - 2 * pad))


def pad_region(region: Tuple[int, int, int, int], margin: float, width: int, height: int) -> Tuple[int, int, int, int]:
    """Grow a plan_region box outward by ``margin`` px, clamped to the image.

    plan_region insets the box by half its dilation, which clips the outer walls;
    the network crop must contain them whole or the outer boundary never closes.
    """
    x, y, w, h = region
    m = int(round(margin))
    x0, y0 = max(0, x - m), max(0, y - m)
    x1, y1 = min(width, x + w + m), min(height, y + h + m)
    return (x0, y0, x1 - x0, y1 - y0)


# ------------------------------------------------------ 3. rectilinear runs
# Narrowest wall band kept, as a fraction of the dominant wall thickness. Thin partitions
# (100-150 mm) beside 230-270 mm walls sit at 0.45-0.55, so 0.5 dropped them by a pixel.
MIN_BAND_FRACTION = 0.45
# A band between MIN_BAND_FRACTION and THIN_BAND_FRACTION of the wall thickness is kept only
# when it runs at least THIN_BAND_MIN_LENGTH wall thicknesses: a partition is long and straight,
# a cushion or fixture outline is short.
THIN_BAND_FRACTION = 0.5
THIN_BAND_MIN_LENGTH = 3.0
# Absolute floor: bands narrower than this are linework (hatch, outlines), whatever the estimate.
MIN_BAND_PX = 4.0


def keep_band(width: float, length: float, thickness: float) -> bool:
    """Is a scanline band of this width and length (px) a wall, given the dominant thickness?"""
    if width < max(thickness * MIN_BAND_FRACTION, MIN_BAND_PX):
        return False
    if width < thickness * THIN_BAND_FRACTION:
        return length >= THIN_BAND_MIN_LENGTH * thickness
    return True


def _same_band(s: float, e: float, band_lo: float, band_hi: float, tol: float, min_overlap: float = 0.8) -> bool:
    """Does a scanline run [s, e) continue a band spanning [band_lo, band_hi)?

    Both ends within ``tol``, or the two spans overlap by ``min_overlap`` of the longer one.
    The overlap test keeps a wall in one band when its edge rows end early where the mask
    tapers into a junction. Without it a thin partition split into a narrow core plus edge
    slivers, and the core fell under the minimum band width (toilet/store wall, 3 Nov 2026)."""
    if abs(s - band_lo) <= tol and abs(e - band_hi) <= tol:
        return True
    overlap = min(e, band_hi) - max(s, band_lo)
    return overlap >= min_overlap * max(e - s, band_hi - band_lo)


def _bands(mask: np.ndarray, min_len: float, thickness: float) -> List[Tuple[float, float, float, float]]:
    """Scanline banding of horizontal runs: (c, lo, hi, t) in mask coords."""
    tol = max(2.0, thickness * 0.5)
    active: List[List[float]] = []  # [x0, x1, y_first, y_last]
    done: List[List[float]] = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            done += active
            active = []
            continue
        padded = np.concatenate([[0], row, [0]]).astype(np.int8)
        d = np.diff(padded)
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        runs = [(s, e) for s, e in zip(starts, ends) if e - s >= min_len]
        next_active = []
        used = set()
        for band in active:
            match = None
            for i, (s, e) in enumerate(runs):
                if i in used:
                    continue
                if _same_band(s, e, band[0], band[1], tol):
                    match = i
                    break
            if match is None:
                done.append(band)
            else:
                used.add(match)
                s, e = runs[match]
                band[0] = min(band[0], s)
                band[1] = max(band[1], e)
                band[3] = y
                next_active.append(band)
        for i, (s, e) in enumerate(runs):
            if i not in used:
                next_active.append([float(s), float(e), float(y), float(y)])
        active = next_active
    done += active
    out = []
    for x0, x1, y0, y1 in done:
        t = y1 - y0 + 1
        if not keep_band(t, x1 - x0, thickness):
            continue
        out.append(((y0 + y1) / 2.0, x0, x1 - 1, t))
    return out


def extract_runs(walls: np.ndarray, thickness: float) -> List[Run]:
    L = max(3, int(round(thickness * 1.6)))
    horizontal = cv2.morphologyEx(walls, cv2.MORPH_OPEN, np.ones((1, L), np.uint8))
    vertical = cv2.morphologyEx(walls, cv2.MORPH_OPEN, np.ones((L, 1), np.uint8))
    runs = [Run("h", c, lo, hi, t) for c, lo, hi, t in _bands(horizontal, L, thickness)]
    runs += [Run("v", c, lo, hi, t) for c, lo, hi, t in _bands(vertical.T, L, thickness)]
    # Drop square-ish blobs (columns, solid furniture) that are not walls.
    return [r for r in runs if (r.hi - r.lo) >= 1.2 * r.t]


# ------------------------------------------------- 4/5. junctions and gaps
def _covers(run: Run, value: float, slack: float) -> bool:
    return run.lo - slack <= value <= run.hi + slack


def _snap_ends(runs: List[Run]) -> Tuple[List[Run], Dict[Tuple[int, str], bool]]:
    """Snap run ends to perpendicular centerlines. Returns runs and a map of
    (run index, 'lo'|'hi') -> joined?"""
    joined: Dict[Tuple[int, str], bool] = {}
    for i, r in enumerate(runs):
        for end in ("lo", "hi"):
            value = getattr(r, end)
            best = None
            for j, p in enumerate(runs):
                if p.axis == r.axis:
                    continue
                if not _covers(p, r.c, r.t / 2 + 1):
                    continue
                dist = abs(p.c - value)
                if dist <= p.t / 2 + r.t * 0.6 and (best is None or dist < best[0]):
                    best = (dist, p.c)
            if best is not None:
                setattr(r, end, best[1])
            joined[(i, end)] = best is not None
    return runs, joined


def _gap_band(dark: np.ndarray, axis: str, c: float, lo: float, hi: float, t: float) -> np.ndarray:
    a, b = int(round(lo)), int(round(hi))
    c0, c1 = int(round(c - t / 2)), int(round(c + t / 2))
    if axis == "h":
        return dark[max(c0, 0):c1 + 1, max(a, 0):b + 1]
    return dark[max(a, 0):b + 1, max(c0, 0):c1 + 1].T


def _is_window(dark: np.ndarray, axis: str, c: float, lo: float, hi: float, t: float) -> bool:
    band = _gap_band(dark, axis, c, lo + 2, hi - 2, t * 0.8)
    if band.size == 0 or band.shape[1] < 3:
        return False
    rows_with_glazing = (band.mean(axis=1) > 0.6).sum()
    return rows_with_glazing >= 1 and band.mean() > 0.05


def _classify_window(dark: np.ndarray, window_mask: Optional[np.ndarray], axis: str,
                     c: float, lo: float, hi: float, t: float) -> bool:
    """Window if a neural window mask covers the gap, else drafted glazing."""
    if window_mask is not None:
        band = _gap_band(window_mask, axis, c, lo, hi, t * 1.5)
        if band.size and band.mean() > 0.2:
            return True
    return _is_window(dark, axis, c, lo, hi, t)


def openings_from_icons(runs: List[Run], openings: List[Opening], window_mask: np.ndarray,
                        door_mask: np.ndarray, min_len: float) -> List[Opening]:
    """Openings the network marked inside continuous walls (no drawn gap)."""
    added: List[Opening] = []
    for kind, mask in (("window", window_mask), ("door", door_mask)):
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        for i in range(1, n):
            x, y, bw, bh, area = stats[i]
            for r in runs:
                if r.axis == "h":
                    lo, hi, mid = max(r.lo, x), min(r.hi, x + bw), y + bh / 2.0
                else:
                    lo, hi, mid = max(r.lo, y), min(r.hi, y + bh), x + bw / 2.0
                if hi - lo < min_len or abs(mid - r.c) > r.t + 4:
                    continue
                if any(o.axis == r.axis and abs(o.c - r.c) <= r.t and o.lo < hi and lo < o.hi
                       for o in openings + added):
                    continue
                added.append(Opening(kind, r.axis, r.c, float(lo), float(hi)))
                break
    return added


def _detect_swing(dark: np.ndarray, op: Opening, t: float) -> None:
    """Look for a quarter-circle door swing on either side of the opening."""
    width = op.hi - op.lo
    best = (0.0, None)
    for hinge in ("start", "end"):
        for side in (-1, 1):  # -1: towards smaller coordinate (up/left in image)
            hits = total = 0
            for k in range(8, 82, 4):
                ang = math.radians(k)
                along = width * math.cos(ang)
                across = width * math.sin(ang)
                pos = op.lo + along if hinge == "start" else op.hi - along
                off = op.c + side * (t / 2 + across - t / 2)
                x, y = (pos, off) if op.axis == "h" else (off, pos)
                xi, yi = int(round(x)), int(round(y))
                if 1 <= yi < dark.shape[0] - 1 and 1 <= xi < dark.shape[1] - 1:
                    total += 1
                    hits += int(dark[yi - 1:yi + 2, xi - 1:xi + 2].any())
            score = hits / total if total else 0.0
            if score > best[0]:
                best = (score, (hinge, side))
    if best[0] >= 0.6 and best[1] is not None:
        hinge, side = best[1]
        op.hinge = hinge
        op.swing_detected = True
        # Image y grows downward. For an 'h' run traversed lo->hi (+x), the
        # left-hand side in y-up CAD space is image side -1 (up). For a 'v'
        # run traversed lo->hi (+y image = -y CAD) the left side is +x (side 1).
        left_side = -1 if op.axis == "h" else 1
        op.swing = "left" if side == left_side else "right"


Segment = Tuple[Point, Point, float]  # start, end, thickness used for room inset


def _xy(axis: str, along: float, c: float) -> Point:
    return (along, c) if axis == "h" else (c, along)


def bridge_openings(
    runs: List[Run], joined: Dict[Tuple[int, str], bool], dark: np.ndarray,
    min_gap: float, max_door: float, max_window: float, max_separator: float,
    window_mask: Optional[np.ndarray] = None,
) -> Tuple[List[Run], List[Opening], List[Segment]]:
    """Close wall gaps. Glazed gaps become windows, door-sized empty gaps
    become doors (both bridged with wall), and wider empty gaps between
    collinear wall pieces become room separators (open plan, no wall)."""
    openings: List[Opening] = []
    separators: List[Segment] = []
    merged_into: Dict[int, int] = {}
    order = sorted(range(len(runs)), key=lambda i: (runs[i].axis, round(runs[i].c), runs[i].lo))
    # Collinear gaps.
    for idx_a in order:
        a = runs[idx_a]
        if idx_a in merged_into:
            continue
        while True:
            candidates = []
            for idx_b, b in enumerate(runs):
                if idx_b == idx_a or idx_b in merged_into or b.axis != a.axis:
                    continue
                if abs(b.c - a.c) > max(a.t, b.t) * 0.5 or abs(b.t - a.t) > 0.45 * max(a.t, b.t):
                    continue
                gap = b.lo - a.hi
                if min_gap <= gap <= max(max_window, max_separator):
                    blocked = any(
                        p.axis != a.axis and a.hi < p.c < b.lo and _covers(p, a.c, 0)
                        for p in runs
                    )
                    if not blocked:
                        candidates.append((gap, idx_b))
            if not candidates:
                break
            gap, idx_b = min(candidates)
            b = runs[idx_b]
            window = gap <= max_window and _classify_window(dark, window_mask, a.axis, a.c, a.hi, b.lo, a.t)
            if not window and gap > max_door:
                separators.append((_xy(a.axis, a.hi, a.c), _xy(a.axis, b.lo, b.c), (a.t + b.t) / 2))
                break
            openings.append(Opening("window" if window else "door", a.axis, (a.c + b.c) / 2, a.hi, b.lo))
            a.hi = b.hi
            a.c = (a.c + b.c) / 2
            a.t = max(a.t, b.t)
            joined[(idx_a, "hi")] = joined.get((idx_b, "hi"), False)
            merged_into[idx_b] = idx_a
    runs_out = [r for i, r in enumerate(runs) if i not in merged_into]
    joined_out = {}
    remap = {old: new for new, old in enumerate(i for i in range(len(runs)) if i not in merged_into)}
    for (i, end), v in joined.items():
        if i in remap:
            joined_out[(remap[i], end)] = v
    sep_ends = {p for seg in separators for p in seg[:2]}
    # Free ends that face a perpendicular wall across a door/window gap.
    for i, r in enumerate(runs_out):
        for end, sign in (("lo", -1), ("hi", 1)):
            if joined_out.get((i, end)) or _xy(r.axis, getattr(r, end), r.c) in sep_ends:
                continue
            value = getattr(r, end)
            best = None
            for p in runs_out:
                if p.axis == r.axis or not _covers(p, r.c, r.t / 2):
                    continue
                gap = (p.c - p.t / 2 - value) if sign > 0 else (value - (p.c + p.t / 2))
                if min_gap <= gap <= max_window and (best is None or gap < best[0]):
                    best = (gap, p)
            if best is None:
                continue
            gap, p = best
            lo, hi = (value, p.c - p.t / 2) if sign > 0 else (p.c + p.t / 2, value)
            window = _classify_window(dark, window_mask, r.axis, r.c, lo, hi, r.t)
            if not window and gap > max_door:
                continue
            openings.append(Opening("window" if window else "door", r.axis, r.c, lo, hi))
            setattr(r, end, p.c)
            joined_out[(i, end)] = True
    for op in openings:
        if op.kind == "door":
            t = next((r.t for r in runs_out if r.axis == op.axis and abs(r.c - op.c) <= r.t), 10.0)
            _detect_swing(dark, op, t)
    return runs_out, openings, separators


def drop_ladders(lines: List[Run], max_gap: float, min_count: int = 3, min_overlap: float = 0.6) -> List[Run]:
    """Drop groups of ``min_count``+ parallel lines that sit close together and overlap along
    their length: stair treads, glazing lines, louvres and hatch. A drawn partition is a single
    line (or a pair at most), so a ladder is never a room boundary, even when both of its ends
    touch a wall."""
    def overlap(a: Run, b: Run) -> bool:
        shared = min(a.hi, b.hi) - max(a.lo, b.lo)
        return shared >= min_overlap * min(a.hi - a.lo, b.hi - b.lo)

    n = len(lines)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = lines[i], lines[j]
            if a.axis == b.axis and abs(a.c - b.c) <= max_gap and overlap(a, b):
                parent[find(i)] = find(j)
    size: Dict[int, int] = {}
    for i in range(n):
        size[find(i)] = size.get(find(i), 0) + 1
    return [ln for i, ln in enumerate(lines) if size[find(i)] < min_count]


def thin_partitions(
    dark: np.ndarray, walls: np.ndarray, runs: List[Run], bbox: Tuple[int, int, int, int],
    min_len: float, thickness: float,
) -> List[Segment]:
    """Long, straight thin lines whose both ends land on a wall or on another
    such line: drawn partitions and slab edges (porch, shaft, toilet
    partition). Stair treads, hatch and furniture lines have free ends and
    are rejected."""
    x0, y0, w, h = bbox
    pad = int(thickness)
    grown_walls = cv2.dilate(walls, np.ones((3, 3), np.uint8))
    thin = (dark & (1 - grown_walls)).astype(np.uint8)
    clip = np.zeros_like(thin)
    clip[max(0, y0 - pad):y0 + h + pad, max(0, x0 - pad):x0 + w + pad] = \
        thin[max(0, y0 - pad):y0 + h + pad, max(0, x0 - pad):x0 + w + pad]
    L = max(10, int(min_len))
    lines: List[Run] = []
    for axis, kernel in (("h", (1, L)), ("v", (L, 1))):
        opened = cv2.morphologyEx(clip, cv2.MORPH_OPEN, np.ones(kernel, np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(opened)
        for i in range(1, n):
            x, y, bw, bh, _ = stats[i]
            if axis == "h" and bh <= max(5, thickness * 0.3):
                lines.append(Run("h", y + bh / 2.0, float(x), float(x + bw - 1), float(bh)))
            elif axis == "v" and bw <= max(5, thickness * 0.3):
                lines.append(Run("v", x + bw / 2.0, float(y), float(y + bh - 1), float(bw)))
    # Drop glazing/door-threshold lines lying inside a wall band.
    def inside_wall(line: Run) -> bool:
        return any(
            r.axis == line.axis and abs(r.c - line.c) <= r.t / 2 + 3
            and min(r.hi, line.hi) - max(r.lo, line.lo) > 0.5 * (line.hi - line.lo)
            for r in runs
        )
    lines = [ln for ln in lines if not inside_wall(ln)]
    lines = drop_ladders(lines, max_gap=thickness * 3.0)
    tol = thickness * 0.6 + 3
    # Posts: small solid blocks (columns) that are not wall runs.
    posts: List[Tuple[float, float, float, float]] = []
    n, _, stats, _ = cv2.connectedComponentsWithStats(walls)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw <= 2.5 * thickness and bh <= 2.5 * thickness and area >= 0.5 * thickness ** 2:
            posts.append((x - tol, y - tol, x + bw + tol, y + bh + tol))

    def anchor(line: Run, value: float, pool: List[Run]) -> Optional[float]:
        """Snap target for a line end: centerline of a perpendicular wall or
        line it touches, or the end itself if it touches a collinear wall."""
        best = None
        for r in runs:
            if r.axis != line.axis and _covers(r, line.c, r.t / 2 + tol) and abs(r.c - value) <= r.t / 2 + tol:
                d = abs(r.c - value)
                if best is None or d < best[0]:
                    best = (d, r.c)
            if r.axis == line.axis and abs(r.c - line.c) <= r.t / 2 + tol and _covers(r, value, tol):
                return value
        for px0, py0, px1, py1 in posts:
            ex, ey = _xy(line.axis, value, line.c)
            if px0 <= ex <= px1 and py0 <= ey <= py1:
                centre = (px0 + px1) / 2 if line.axis == "h" else (py0 + py1) / 2
                d = abs(centre - value)
                if best is None or d < best[0]:
                    best = (d, centre)
        for other in pool:
            if other is line:
                continue
            if other.axis != line.axis and _covers(other, line.c, tol) and abs(other.c - value) <= tol:
                d = abs(other.c - value)
                if best is None or d < best[0]:
                    best = (d, other.c)
        return None if best is None else best[1]

    pool = list(lines)
    while True:
        keep = [ln for ln in pool if anchor(ln, ln.lo, pool) is not None and anchor(ln, ln.hi, pool) is not None]
        if len(keep) == len(pool):
            break
        pool = keep
    out: List[Segment] = []
    for ln in pool:
        lo, hi = anchor(ln, ln.lo, pool), anchor(ln, ln.hi, pool)
        out.append((_xy(ln.axis, lo, ln.c), _xy(ln.axis, hi, ln.c), 1.0))
    return out


# --------------------------------------------------------------- 6. labels
FEET_INCH_RE = re.compile(
    r"(\d{1,2})\s*['’‘′`]\s*(?:-?\s*(\d{1,2}(?:\.\d+)?)\s*(?:[\"”″“]|''|in\b)?)?"
    r"\s*[xX×*]\s*"
    r"(\d{1,2})\s*['’‘′`]\s*(?:-?\s*(\d{1,2}(?:\.\d+)?)\s*(?:[\"”″“]|''|in\b)?)?"
)

METRE_RE = re.compile(
    r"(?:^|[^\d.])"
    r"(\d+(?:[.,]\d+)?)\s*(?:m\b|M\b)?"
    r"\s*[xX×*]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:m\b|M\b)?"
)

SIZE_CM_RE = re.compile(r"(\d{2,4})\s*(?:cm\b|CM\b)?\s*[xX×*]\s*(\d{2,4})\s*(?:cm\b|CM\b)?")
SIZE_MM_RE = re.compile(r"(\d{3,5})\s*(?:mm\b|MM\b)\s*[xX×*]\s*(\d{3,5})\s*(?:mm\b|MM\b)?")
SIZE_RE = re.compile(r"(\d{2,4})\s*[xX×*]\s*(\d{2,4})")


def parse_size(text: str) -> Optional[Tuple[float, float]]:
    """Extract (w_cm, h_cm) if text matches feet-inches (12'x13', 12'6"x10'),
    metres (3.30X3.40, 3.3 x 3.4), millimetres (3300mm x 3400mm), or centimetres,
    converting all to cm and enforcing the 50-1500 cm sanity bounds."""
    clean = text.strip()

    # 1. Feet and inches (e.g. 12'x13', 12'6"x10', 12'-6" x 10'-0", 12’6” x 10’)
    m = FEET_INCH_RE.search(clean)
    if m:
        ft1 = float(m.group(1))
        in1 = float(m.group(2)) if m.group(2) else 0.0
        ft2 = float(m.group(3))
        in2 = float(m.group(4)) if m.group(4) else 0.0
        cm1 = ft1 * 30.48 + in1 * 2.54
        cm2 = ft2 * 30.48 + in2 * 2.54
        if 50.0 <= cm1 <= 1500.0 and 50.0 <= cm2 <= 1500.0:
            return (round(cm1, 1), round(cm2, 1))

    # 2. Metres (e.g. 3.30X3.40, 3.3 x 3.4, 3.30m x 3.40m, 3,30 x 3,40)
    # Must have decimal point or explicit 'm' unit so integer feet/cm are not confused.
    for match in METRE_RE.finditer(clean):
        s1 = match.group(1).replace(",", ".")
        s2 = match.group(2).replace(",", ".")
        has_decimal = ("." in s1) or ("." in s2)
        has_unit = bool(re.search(r"[mM]", match.group(0)))
        if has_decimal or has_unit:
            try:
                v1, v2 = float(s1), float(s2)
                cm1, cm2 = v1 * 100.0, v2 * 100.0
                if 50.0 <= cm1 <= 1500.0 and 50.0 <= cm2 <= 1500.0:
                    return (round(cm1, 1), round(cm2, 1))
            except ValueError:
                pass

    # 3. Explicit Millimetres (e.g. 3300mm x 3400mm)
    m = SIZE_MM_RE.search(clean)
    if m:
        v1, v2 = float(m.group(1)) / 10.0, float(m.group(2)) / 10.0
        if 50.0 <= v1 <= 1500.0 and 50.0 <= v2 <= 1500.0:
            return (round(v1, 1), round(v2, 1))

    # 4. Centimetres (e.g. 355 x 400, 360 X 380)
    m = SIZE_CM_RE.search(clean) or SIZE_CM_RE.search(clean.replace(" ", ""))
    if m:
        v1, v2 = float(m.group(1)), float(m.group(2))
        if 50.0 <= v1 <= 1500.0 and 50.0 <= v2 <= 1500.0:
            return (round(v1, 1), round(v2, 1))

    return None


def _text_only(gray: np.ndarray, walls: Optional[np.ndarray], thickness: float) -> np.ndarray:
    """Whiten walls and long straight lines so OCR sees only the lettering."""
    dark = _dark_mask(gray)
    erase = np.zeros_like(dark)
    if walls is not None:
        erase |= cv2.dilate(walls, np.ones((5, 5), np.uint8))
    L = max(25, int(thickness * 2.2))  # longer than any glyph stroke
    for kernel in ((1, L), (L, 1)):
        erase |= cv2.dilate(cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones(kernel, np.uint8)), np.ones((3, 3), np.uint8))
    out = gray.copy()
    out[erase > 0] = 255
    return out


def parse_labels_from_data(
    data: Dict[str, Sequence[Any]],
    scale: float = 1.0,
    offset_x: int = 0,
    offset_y: int = 0,
) -> List[Label]:
    """Build lines and labels with paired room sizes from tesseract image_to_data."""
    words = []
    n_items = len(data.get("text", []))
    for i in range(n_items):
        text = str(data["text"][i] or "").strip()
        conf = float(data["conf"][i]) if "conf" in data else 100.0
        if not text or conf < 30:
            continue
        bx = int(data["left"][i] / scale) + offset_x
        by = int(data["top"][i] / scale) + offset_y
        bw = int(data["width"][i] / scale)
        bh = int(data["height"][i] / scale)
        words.append({"text": text, "box": (bx, by, bw, bh)})

    # Group words into lines (similar baseline, close horizontally).
    words.sort(key=lambda w: (w["box"][1], w["box"][0]))
    lines: List[Dict[str, Any]] = []
    for word in words:
        x, y, w, h = word["box"]
        for line in lines:
            lx, ly, lw, lh = line["box"]
            if (
                abs((y + h / 2) - (ly + lh / 2)) < 0.6 * max(h, lh)
                and x - (lx + lw) < 3.0 * max(h, lh)
                and x >= lx - h
            ):
                nx0, ny0 = min(lx, x), min(ly, y)
                nx1, ny1 = max(lx + lw, x + w), max(ly + lh, y + h)
                line["box"] = (nx0, ny0, nx1 - nx0, ny1 - ny0)
                line["words"].append(word)
                break
        else:
            lines.append({"box": (x, y, w, h), "words": [word]})

    # a) After grouping words into a line, sort the line's words by x before joining.
    for line in lines:
        line["words"].sort(key=lambda w: w["box"][0])
        line["text"] = " ".join(w["text"] for w in line["words"])

    labels: List[Label] = []
    used_size_lines = set()
    for i, line in enumerate(lines):
        text = line["text"]
        if not re.search(r"[A-Za-z]{3,}", text):
            continue
        letters = " ".join(w["text"] for w in line["words"] if re.search(r"[A-Za-z]{2,}", w["text"]))
        if not letters:
            continue
        x, y, w, h = line["box"]
        size = parse_size(text)
        if size is None:
            # Candidate size lines: directly below the name line, within 2.5 text heights, overlapping horizontally
            cand_indices = []
            for j, other in enumerate(lines):
                if j == i or j in used_size_lines:
                    continue
                ox, oy, ow, oh = other["box"]
                if 0 < oy - y <= 2.5 * h and max(x, ox) < min(x + w, ox + ow):
                    # Attach only to the nearest name line directly above it
                    is_nearest = True
                    for k, other_name in enumerate(lines):
                        if k == i:
                            continue
                        if not re.search(r"[A-Za-z]{3,}", other_name["text"]):
                            continue
                        k_y = other_name["box"][1]
                        if y < k_y <= oy and max(other_name["box"][0], ox) < min(other_name["box"][0] + other_name["box"][2], ox + ow):
                            is_nearest = False
                            break
                    if is_nearest:
                        cand_indices.append(j)
            cand_indices.sort(key=lambda j: lines[j]["box"][1])

            # Try single candidate line first
            for cidx in cand_indices:
                sz = parse_size(lines[cidx]["text"])
                if sz is not None:
                    size = sz
                    used_size_lines.add(cidx)
                    break

            # Handle OCR splitting "360 x 380" across two lines
            if size is None and len(cand_indices) >= 2:
                for k in range(len(cand_indices) - 1):
                    i1, i2 = cand_indices[k], cand_indices[k + 1]
                    t1, t2 = lines[i1]["text"], lines[i2]["text"]
                    sz = parse_size(f"{t1} {t2}") or parse_size(f"{t1} x {t2}")
                    if sz is not None:
                        size = sz
                        used_size_lines.add(i1)
                        used_size_lines.add(i2)
                        break

        labels.append(Label(letters.title(), (x, y, w, h), size))
    return labels


def read_labels(gray: np.ndarray, bbox: Tuple[int, int, int, int],
                walls: Optional[np.ndarray] = None, thickness: float = 20.0) -> List[Label]:
    try:
        import os
        import shutil
        import pytesseract

        if not shutil.which("tesseract"):
            default_win_tess = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if os.path.exists(default_win_tess):
                pytesseract.pytesseract.tesseract_cmd = default_win_tess
    except ImportError:  # pragma: no cover
        return []
    gray = _text_only(gray, walls, thickness)
    x0, y0, w, h = bbox
    pad = 10
    crop = gray[max(0, y0 - pad):y0 + h + pad, max(0, x0 - pad):x0 + w + pad]
    scale = 2.0 if max(crop.shape) < 2500 else 1.0
    big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    try:
        data = pytesseract.image_to_data(big, config="--psm 11", output_type=pytesseract.Output.DICT)
    except Exception:  # pragma: no cover - tesseract binary missing
        return []
    return parse_labels_from_data(data, scale=scale, offset_x=max(0, x0 - pad), offset_y=max(0, y0 - pad))


# ------------------------------------------------------------- 7. assembly
def _runs_to_trace(runs: List[Run], openings: List[Opening], labels: List[Label], mm_per_px: float,
                   separators: Sequence[Segment] = ()) -> TraceResult:
    nodes: Dict[Tuple[int, int], str] = {}

    def node(x: float, y: float) -> str:
        key = (int(round(x * 2)), int(round(y * 2)))
        if key not in nodes:
            nodes[key] = f"n{len(nodes) + 1}"
        return nodes[key]

    node_xy: Dict[str, Tuple[float, float]] = {}
    edges = []
    for r in runs:
        a = (r.lo, r.c) if r.axis == "h" else (r.c, r.lo)
        b = (r.hi, r.c) if r.axis == "h" else (r.c, r.hi)
        ia, ib = node(*a), node(*b)
        node_xy[ia], node_xy[ib] = a, b
        if ia != ib:
            edges.append({"source": ia, "target": ib, "type": "internal", "thickness": round(r.t, 2)})
    for a, b, t in separators:
        ia, ib = node(*a), node(*b)
        node_xy[ia], node_xy[ib] = a, b
        if ia != ib:
            edges.append({"source": ia, "target": ib, "type": "separator", "thickness": round(t, 2)})
    doors, windows = [], []
    for op in openings:
        center = ((op.lo + op.hi) / 2, op.c) if op.axis == "h" else (op.c, (op.lo + op.hi) / 2)
        host = next(
            (r for r in runs if r.axis == op.axis and abs(r.c - op.c) <= r.t and r.lo - 1 <= op.lo and op.hi <= r.hi + 1),
            None,
        )
        if host is None:
            continue
        a = (host.lo, host.c) if host.axis == "h" else (host.c, host.lo)
        b = (host.hi, host.c) if host.axis == "h" else (host.c, host.hi)
        item = {
            "edge_nodes": [node(*a), node(*b)],
            "length": round(op.hi - op.lo, 2),
            "center": [round(center[0], 2), round(center[1], 2)],
        }
        if op.kind == "door":
            item.update(hinge=op.hinge, swing=op.swing)
            doors.append(item)
        else:
            windows.append(item)
    zones = [{"name": lab.text, "seedPoint": list(lab.center)} for lab in labels]
    return TraceResult.model_validate(
        {
            "walls_graph": {
                "nodes": [{"id": i, "x": x, "y": y} for (i, (x, y)) in node_xy.items()],
                "edges": edges,
            },
            "doors_list": doors,
            "windows_list": windows,
            "zones_list": zones,
            "units": "px",
            "mm_per_unit": mm_per_px,
            "y_down": True,
            "source": "trace_engine.vision",
        }
    )


def _mark_external(trace: TraceResult, shape: Tuple[int, int]) -> None:
    """External walls are those with the outside on one side."""
    from PIL import Image, ImageDraw

    im = Image.new("L", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(im)
    xy = {n.id: (n.x, n.y) for n in trace.walls_graph.nodes}
    for e in trace.walls_graph.edges:
        if e.type == "separator":
            continue
        a, b = xy[e.source], xy[e.target]
        draw.line(
            [(int(round(a[0])), int(round(a[1]))), (int(round(b[0])), int(round(b[1])))],
            fill=1,
            width=max(1, int(round(e.thickness))),
        )
    ImageDraw.floodfill(im, (0, 0), 2)
    outside = np.asarray(im) == 2
    for e in trace.walls_graph.edges:
        if e.type == "separator":
            continue
        a, b = xy[e.source], xy[e.target]
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1
        nx, ny = -dy / length, dx / length
        probe = e.thickness / 2 + 3
        for s in (-1, 1):
            px_, py_ = int(round(mx + s * nx * probe)), int(round(my + s * ny * probe))
            if 0 <= py_ < shape[0] and 0 <= px_ < shape[1] and outside[py_, px_]:
                e.type = "external"


def _traced_rooms_px(trace_px: TraceResult, thickness_px: float):
    provisional = trace_px.model_copy(update={"units": "mm", "mm_per_unit": 1.0, "y_down": False})
    network = clean(provisional, snap_tolerance_mm=max(2.0, thickness_px * 0.4))
    rooms, _ = recover_rooms(network, (), min_area_mm2=(thickness_px * 3) ** 2,
                             min_clear_dimension_mm=thickness_px * 1.5)
    return rooms


def _inside_point(poly: Sequence[Point]) -> Point:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    centre = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    if point_in_polygon(centre, poly):
        return centre
    best = None
    for i in range(len(poly)):  # midpoints of diagonals: one is inside
        for j in range(i + 2, len(poly)):
            m = ((poly[i][0] + poly[j][0]) / 2, (poly[i][1] + poly[j][1]) / 2)
            if point_in_polygon(m, poly):
                return m
            best = best or m
    return best or centre


def fit_room_sizes(trace_px: TraceResult, sizes: Sequence[Tuple[str, float, float]],
                   thickness_px: float, tolerance: float = 0.05) -> List[Label]:
    """Place known room sizes ("Kitchen", 320, 307 cm) on traced rooms
    without OCR: find the scale most sizes agree on, then assign each size
    to the room it fits best. Returns position-bearing labels."""
    rooms = _traced_rooms_px(trace_px, thickness_px)
    dims = []
    for room in rooms:
        xs = [p[0] for p in room.clear]
        ys = [p[1] for p in room.clear]
        dims.append((max(xs) - min(xs), max(ys) - min(ys)))
    candidates = []  # (k, room index, size index)
    for si, (_, a_cm, b_cm) in enumerate(sizes):
        a, b = a_cm * 10, b_cm * 10
        for ri, (w, h) in enumerate(dims):
            if w <= 0 or h <= 0:
                continue
            for k1, k2 in ((a / w, b / h), (a / h, b / w)):
                if abs(k1 - k2) / max(k1, k2) <= 2 * tolerance:
                    candidates.append(((k1 + k2) / 2, ri, si))
    if not candidates:
        return []

    def support(k: float) -> int:
        return len({si for kk, _, si in candidates if abs(kk - k) / k <= tolerance})

    k_star = max((c[0] for c in candidates), key=support)
    near = [c for c in candidates if abs(c[0] - k_star) / k_star <= tolerance]
    k_star = float(np.median([c[0] for c in near]))
    labels: List[Label] = []
    used_rooms, used_sizes = set(), set()
    for k, ri, si in sorted(near, key=lambda c: abs(c[0] - k_star)):
        if ri in used_rooms or si in used_sizes:
            continue
        used_rooms.add(ri)
        used_sizes.add(si)
        x, y = _inside_point(rooms[ri].clear)
        name, a_cm, b_cm = sizes[si]
        labels.append(Label(name, (int(x), int(y), 0, 0), (float(a_cm), float(b_cm))))
    return labels


def calibrate_scale(trace_px: TraceResult, labels: List[Label], thickness_px: float,
                    nominal_wall_mm: float) -> Tuple[float, str, List[Dict[str, Any]]]:
    """Fit mm-per-pixel from labelled room sizes (cm) against traced rooms."""
    fallback_scale = nominal_wall_mm / thickness_px if (math.isfinite(thickness_px) and thickness_px > 0) else 10.0
    fallback_method = f"nominal {nominal_wall_mm:.0f} mm wall thickness"

    size_labels = [lab for lab in labels if lab.size_cm]
    if len(size_labels) < 2:
        return fallback_scale, fallback_method, []
    try:
        provisional = trace_px.model_copy(update={"units": "mm", "mm_per_unit": 1.0, "y_down": False})
        network = clean(provisional, snap_tolerance_mm=max(2.0, thickness_px * 0.4))
        rooms, _ = recover_rooms(network, (), min_area_mm2=(thickness_px * 3) ** 2,
                                 min_clear_dimension_mm=thickness_px * 1.5)
    except Exception:
        return fallback_scale, fallback_method, []

    samples = []
    for lab in labels:
        if not lab.size_cm:
            continue
        host = [r for r in rooms if point_in_polygon(lab.center, r.centerline)]
        if not host:
            continue
        room = min(host, key=lambda r: r.area_mm2)
        xs = [p[0] for p in room.clear]
        ys = [p[1] for p in room.clear]
        w_px, h_px = max(xs) - min(xs), max(ys) - min(ys)
        if w_px <= 0 or h_px <= 0:
            continue
        a_mm, b_mm = lab.size_cm[0] * 10, lab.size_cm[1] * 10
        pairs = [(a_mm / w_px, b_mm / h_px), (a_mm / h_px, b_mm / w_px)]
        k1, k2 = min(pairs, key=lambda p: abs(p[0] - p[1]) / max(p))
        if abs(k1 - k2) / max(k1, k2) < 0.25:
            samples.append({"label": lab.text, "size_cm": lab.size_cm,
                            "clear_px": (round(w_px, 1), round(h_px, 1)),
                            "mm_per_px": round((k1 + k2) / 2, 3)})
    if len(samples) >= 2:
        median = float(np.median([s["mm_per_px"] for s in samples]))
        for s in samples:
            s["used"] = abs(s["mm_per_px"] - median) / median <= 0.05
        used = [s["mm_per_px"] for s in samples if s["used"]]
        if len(used) >= 2:
            fitted = float(np.median(used))
            if math.isfinite(fitted) and fitted > 0:
                return fitted, f"room size labels ({len(used)} of {len(samples)} rooms, median)", samples
        elif len(used) == 1:
            fitted = float(used[0])
            if math.isfinite(fitted) and fitted > 0:
                return fitted, f"room size labels (1 of {len(samples)} rooms)", samples
    return fallback_scale, fallback_method, samples


def trace_image(
    image_path: str,
    *,
    mm_per_px: Optional[float] = None,
    nominal_wall_mm: float = 230.0,
    ocr: bool = True,
    max_door_mm: float = 1500.0,
    max_window_mm: float = 3600.0,
    max_separator_mm: float = 6000.0,
    min_partition_mm: float = 1500.0,
    room_sizes: Optional[Sequence[Tuple[str, float, float]]] = None,
    engine: str = "classic",
    weights: Optional[str] = None,
    wall_logit_bias: Optional[float] = None,
    stages: Optional[Dict[str, Any]] = None,
    close_rooms: Optional[bool] = None,
) -> Tuple[TraceResult, VisionReport]:
    """Recognise a raster floor plan. Returns the trace (pixel units, y-down,
    calibrated ``mm_per_unit``) and a report with intermediate results.

    ``engine``: "classic" finds walls as solid dark fill; "neural" uses the
    CubiCasa5K network (PyTorch + weights) for walls, windows and doors.
    ``wall_logit_bias`` (neural only) trades wall recall against precision;
    ``None`` uses the promoted value from ``models/neural_profile.json`` when
    the weights are the promoted ones, else 0.

    ``close_rooms`` (neural only): add separators that close rooms which the raw
    network masks enclose but the traced walls leave open or merged (see
    ``room_closure.py``). ``None`` uses ``close_rooms`` from
    ``models/neural_profile.json`` when the weights are the promoted ones, else off.

    ``stages``: pass an empty dict to receive the intermediate products (raw wall mask, opened
    walls, extracted and bridged runs, openings, thickness) for ``scripts/stage_trace.py``."""
    import copy

    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read image {image_path}")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    dark = drop_frame(_dark_mask(gray))
    window_mask = door_mask = None
    room_map = raw_wall_mask = None
    closure_notes: List[Dict[str, Any]] = []
    if engine == "neural":
        from .neural import find_weights, segment

        path = find_weights(weights)
        if path is None:
            raise RuntimeError("CubiCasa5K weights not found (model_best_val_loss_var.pkl)")
        try:  # crop to the drawing so margins/banners don't waste the network
            t0 = estimate_wall_thickness(dark)
            region = plan_region(wall_mask(gray, t0) & dark, t0)
            ih, iw = gray.shape[:2]
            region = pad_region(region, max(24.0, t0 * 4.0), iw, ih)
            if (region[2] * region[3]) < 0.20 * (iw * ih):
                region = None
        except ValueError:
            region = None
        from .neural import default_wall_bias

        if wall_logit_bias is None:
            wall_logit_bias = default_wall_bias(path)
        seg = segment(image, region, path, wall_logit_bias=wall_logit_bias)
        window_mask, door_mask = seg["windows"], seg["doors"]
        if close_rooms is None:
            from .neural import default_close_rooms

            close_rooms = default_close_rooms(path)
        if close_rooms and seg.get("rooms") is not None:
            room_map, raw_wall_mask = np.asarray(seg["rooms"]), np.asarray(seg["walls"])
        thickness, thickness_source = mask_wall_thickness(seg["walls"], min_px=4)
        k = max(3, int(round(thickness * 0.5)))
        walls = cv2.morphologyEx(seg["walls"], cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        if stages is not None:
            stages.update(raw_walls=np.asarray(seg["walls"]).astype(np.uint8),
                          raw_windows=np.asarray(seg["windows"]).astype(np.uint8),
                          raw_doors=np.asarray(seg["doors"]).astype(np.uint8),
                          weights_path=str(path), wall_logit_bias=float(wall_logit_bias),
                          thickness_source=thickness_source,
                          network_region=list(region) if region is not None else None)
    else:
        try:
            thickness = estimate_wall_thickness(dark)
            walls = wall_mask(gray, thickness) & dark
        except ValueError:
            thickness = 10.0
            walls = np.zeros_like(gray)

    try:
        bbox = plan_region(walls, thickness)
    except ValueError:
        bbox = None

    if bbox is None:
        empty_scale = nominal_wall_mm / thickness if (math.isfinite(thickness) and thickness > 0) else 10.0
        empty_trace = TraceResult(
            walls_graph=WallsGraph(nodes=[], edges=[]),
            doors_list=[],
            windows_list=[],
            zones_list=[],
            units="px",
            mm_per_unit=empty_scale,
            y_down=True,
            source="trace_engine.vision",
        )
        report = VisionReport(
            (gray.shape[1], gray.shape[0]),
            (0, 0, gray.shape[1], gray.shape[0]),
            thickness if math.isfinite(thickness) else 10.0,
            empty_scale,
            f"nominal {nominal_wall_mm:.0f} mm wall thickness",
            [],
            [],
            read_labels(gray, (0, 0, gray.shape[1], gray.shape[0]), walls, thickness) if ocr else [],
            [],
        )
        report.warnings.append("no walls found")
        report.engine = engine
        report.separators = []
        if stages is not None:
            stages.update(dark=dark, walls=walls, clip=walls, raw_runs=[], runs=[], openings=[], separators=[],
                          thickness=thickness, bbox=bbox or (0, 0, gray.shape[1], gray.shape[0]),
                          mm_per_px=empty_scale, early_exit="no wall cluster (plan_region found none)")
            stages.setdefault("raw_walls", dark)
        return empty_trace, report

    # plan_region insets its box by half its dilation (1.5 wall thicknesses), which
    # would shave the outer walls off: clip with the box grown back out.
    x0, y0, w, h = pad_region(bbox, thickness * 2.0, walls.shape[1], walls.shape[0])
    clip = np.zeros_like(walls)
    clip[y0:y0 + h, x0:x0 + w] = walls[y0:y0 + h, x0:x0 + w]

    raw_runs = extract_runs(clip, thickness)
    if not raw_runs:
        empty_scale = nominal_wall_mm / thickness if (math.isfinite(thickness) and thickness > 0) else 10.0
        empty_trace = TraceResult(
            walls_graph=WallsGraph(nodes=[], edges=[]),
            doors_list=[],
            windows_list=[],
            zones_list=[],
            units="px",
            mm_per_unit=empty_scale,
            y_down=True,
            source="trace_engine.vision",
        )
        report = VisionReport(
            (gray.shape[1], gray.shape[0]),
            bbox,
            thickness if math.isfinite(thickness) else 10.0,
            empty_scale,
            f"nominal {nominal_wall_mm:.0f} mm wall thickness",
            [],
            [],
            read_labels(gray, bbox, walls, thickness) if ocr else [],
            [],
        )
        report.warnings.append("no walls found")
        report.engine = engine
        report.separators = []
        if stages is not None:
            stages.update(dark=dark, walls=walls, clip=walls, raw_runs=[], runs=[], openings=[], separators=[],
                          thickness=thickness, bbox=bbox or (0, 0, gray.shape[1], gray.shape[0]),
                          mm_per_px=empty_scale, early_exit="no straight wall runs extracted")
            stages.setdefault("raw_walls", dark)
        return empty_trace, report

    base_runs, base_joined = _snap_ends(raw_runs)
    labels = read_labels(gray, bbox, walls, thickness) if ocr else []
    need_fit = bool(room_sizes) and sum(1 for lab in labels if lab.size_cm) < 2

    def build(scale: float):
        runs, joined = copy.deepcopy(base_runs), dict(base_joined)
        min_gap = max(thickness * 0.8, 450.0 / scale)
        runs, openings, separators = bridge_openings(
            runs, joined, dark, min_gap, max_door_mm / scale, max_window_mm / scale,
            max_separator_mm / scale, window_mask,
        )
        if window_mask is not None:
            openings += openings_from_icons(runs, openings, window_mask, door_mask, 450.0 / scale)
        separators += thin_partitions(dark, walls, runs, bbox, min_partition_mm / scale, thickness)
        if room_map is not None:
            from . import room_closure

            found = room_closure.close_rooms(room_map, raw_wall_mask, runs, separators, scale,
                                             window_mask=window_mask, door_mask=door_mask, thickness=thickness)
            separators += found.separators
            closure_notes[:] = found.notes
        trace = _runs_to_trace(runs, openings, labels, scale, separators)
        _mark_external(trace, gray.shape)
        return trace, runs, openings, separators

    if mm_per_px is not None:
        scale, method, samples = mm_per_px, "given", []
        trace_px, runs, openings, separators = build(scale)
        if need_fit:
            labels = fit_room_sizes(trace_px, room_sizes, thickness)
            trace_px, runs, openings, separators = build(scale)
    else:
        # Pass 1 at the nominal scale; refit; pass 2 if the scale moved.
        scale = nominal_wall_mm / thickness if (math.isfinite(thickness) and thickness > 0) else 10.0
        trace_px, runs, openings, separators = build(scale)
        if need_fit:
            labels = fit_room_sizes(trace_px, room_sizes, thickness)
            trace_px, runs, openings, separators = build(scale)
        fitted, method, samples = calibrate_scale(trace_px, labels, thickness, nominal_wall_mm)
        if not math.isfinite(fitted) or fitted <= 0:
            fitted = scale
            method = f"nominal {nominal_wall_mm:.0f} mm wall thickness"
        if room_sizes and method.startswith("nominal"):
            # OCR read labels but no usable sizes (garbled text, labels outside
            # closed rooms): the sizes the user gave are authoritative, so fit
            # them to the traced rooms directly and name the rooms from them.
            fit_labels = fit_room_sizes(trace_px, room_sizes, thickness)
            if len(fit_labels) >= 2:
                k, m, smp = calibrate_scale(trace_px, fit_labels, thickness, nominal_wall_mm)
                if not m.startswith("nominal") and math.isfinite(k) and k > 0:
                    fitted, method, samples = k, f"given room sizes ({m})", smp
                    labels = fit_labels
        if abs(fitted - scale) / scale > 0.03:
            trace_px, runs, openings, separators = build(fitted)
            fitted, method, samples = calibrate_scale(trace_px, labels, thickness, nominal_wall_mm)
            if not math.isfinite(fitted) or fitted <= 0:
                fitted = scale
                method = f"nominal {nominal_wall_mm:.0f} mm wall thickness"
        scale = fitted
    if not math.isfinite(scale) or scale <= 0:
        scale = nominal_wall_mm / thickness if (math.isfinite(thickness) and thickness > 0) else 10.0
    trace = trace_px.model_copy(update={"mm_per_unit": scale})
    report = VisionReport((gray.shape[1], gray.shape[0]), bbox, thickness, scale, method,
                          runs, openings, labels, samples)
    report.engine = engine
    report.separators = separators
    report.wall_logit_bias = wall_logit_bias if engine == "neural" else None
    report.room_closures = list(closure_notes)
    if stages is not None:
        stages["room_closures"] = list(closure_notes)
        stages.update(dark=dark, walls=walls, clip=clip, raw_runs=raw_runs, runs=runs, openings=openings,
                      separators=separators, thickness=thickness, bbox=bbox, mm_per_px=scale)
        stages.setdefault("raw_walls", dark)
    if not labels and ocr:
        report.warnings.append("no room labels read (OCR unavailable or no text); rooms named 'Space N'")
    return trace, report


def render_overlay(image_path: str, trace: TraceResult, compiled: Any, out_path: str) -> str:
    """Draw the cleaned trace over the source image for visual checking."""
    from PIL import Image, ImageDraw

    with Image.open(image_path) as im:
        im = im.convert("RGB")
    white = Image.new("RGB", im.size, (255, 255, 255))
    overlay = Image.blend(im, white, 0.65)
    draw = ImageDraw.Draw(overlay)

    scale = trace.mm_per_unit if (math.isfinite(trace.mm_per_unit) and trace.mm_per_unit > 0) else 10.0
    dx, dy = compiled.offset

    def moved_mm(p):  # cleaned network mm -> compiled (shifted) mm
        return (p[0] + dx, p[1] + dy)

    def to_px(p):  # compiled mm (y-up, shifted) -> image px (y-down)
        return (int(round((p[0] - dx) / scale)), int(round(-(p[1] - dy) / scale)))

    walls = {c["@id"]: c for c in compiled.changes if c["@type"] == "top:Wall"}
    for c in compiled.changes:
        if c["@type"] == "top:Room":
            pts = [to_px(p) for p in c["geometry"]["boundary"]]
            if len(pts) >= 3:
                draw.polygon(pts, fill=(235, 245, 225), outline=(60, 160, 60))
    for c in walls.values():
        g = c["geometry"]
        color = (200, 40, 40) if c["label"].startswith("External") else (40, 80, 200)
        t = max(1, int(round(g["thickness"] / scale * 0.35)))
        p0, p1 = to_px(g["start"]), to_px(g["end"])
        draw.line([p0, p1], fill=color, width=t)
        for p in (p0, p1):
            draw.ellipse([p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3], fill=(0, 0, 0))
    net = compiled.network
    for wall in net.walls:
        if wall.type == "separator":
            p0, p1 = to_px(moved_mm(net.nodes[wall.a])), to_px(moved_mm(net.nodes[wall.b]))
            length = max(1, int(math.dist(p0, p1)))
            for k in range(0, length, 12):
                q0 = (p0[0] + (p1[0] - p0[0]) * k // length, p0[1] + (p1[1] - p0[1]) * k // length)
                k2 = min(length, k + 7)
                q1 = (p0[0] + (p1[0] - p0[0]) * k2 // length, p0[1] + (p1[1] - p0[1]) * k2 // length)
                draw.line([q0, q1], fill=(150, 60, 150), width=2)
    for c in compiled.changes:
        if c["@type"] in ("top:Door", "top:Window"):
            g = c["geometry"]
            host = walls.get(g.get("host_wall_id"))
            if not host:
                continue
            w = host["geometry"]
            a, b = np.array(w["start"]), np.array(w["end"])
            norm = np.linalg.norm(b - a)
            if norm == 0:
                continue
            u = (b - a) / norm
            p0, p1 = a + u * g["offset"], a + u * (g["offset"] + g["width"])
            color = (0, 170, 0) if c["@type"] == "top:Door" else (0, 170, 220)
            draw.line([to_px(p0), to_px(p1)], fill=color, width=6)
    for c in compiled.changes:
        if c["@type"] == "top:Room":
            pts = c["geometry"]["boundary"]
            if pts:
                cx = sum(p[0] for p in pts) / len(pts)
                cy = sum(p[1] for p in pts) / len(pts)
                x, y = to_px((cx, cy))
                draw.text((x - 40, y), c["label"][:18], fill=(20, 90, 20))
    overlay.save(out_path)
    return out_path
