"""Register IFC storeys (mm) onto plan images (px) and match images to storeys.

For one image and one storey:

1. image evidence: long straight dark strokes (walls, solid or double-line;
   hatching, furniture and text are filtered out) and their distance
   transform;
2. model evidence: points sampled along the wall outlines, skipping the
   stretches covered by openings (doors are gaps in the drawing);
3. coarse search over 8 poses (rotation 0/90/180/270 x mirror) and a log
   scale sweep, translation by template correlation at low resolution;
4. refinement: trimmed ICP against the nearest line pixel with a 4-DOF
   similarity (scale, small rotation, translation), at ~1000 px;
5. score: F-measure of precision (model points within 3 px of a line,
   ``inlier``) and recall (line work under the model), normalised against
   the same model shifted off-register (``baseline``) so busy drawings
   don't score well by chance.

``match_folder`` scores every image against every storey and assigns them
1:1 with the Hungarian method, then buckets each pair as ``auto_ok``,
``review`` or ``reject``.

Requires numpy + OpenCV; uses scipy when present (faster nearest-pixel
lookup and assignment), with pure-numpy fallbacks.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from ._cv import cv2
from .ifc_groundtruth import StoreyGT
from .rooms import point_in_polygon

Point = Tuple[float, float]

COARSE_SIDE = 220
REFINE_SIDE = 1000
POSES = [(rot, mirror) for mirror in (False, True) for rot in (0, 90, 180, 270)]
# Tracers that work in the image's own frame (e.g. Synaps) export storeys
# unrotated with y pointing down the image: try that pose first.
IMAGE_FRAME_POSES = [(0, True)]
# Share of the image covered by long straight strokes. Line drawings sit at
# ~0.02-0.07; colour renders and photos are far above.
MAX_LINE_DENSITY = 0.10


# ------------------------------------------------------------- results
@dataclass
class Registration:
    image: str
    storey: str
    image_size: Tuple[int, int]  # width, height (original image)
    matrix: List[List[float]]  # 2x3, drawing mm (IFC x, y) -> original image px
    rotation: int
    mirror: bool
    px_per_mm: float
    inlier: float  # precision: model outline points on drawn lines
    baseline: float  # F-measure when shifted off-register
    score: float  # (F - baseline) / (1 - baseline)
    recall: float = 0.0  # drawn line work explained by the model
    support: float = 0.0  # share of wall faces drawn along most of their length
    margin: float = 0.0  # score gap to the next-best storey for this image
    bucket: str = "review"
    notes: List[str] = field(default_factory=list)

    @property
    def match(self) -> float:
        """Combined evidence used to rank storeys for an image."""
        return 0.5 * (self.score + self.support)

    def to_px(self, p: Sequence[float]) -> Point:
        m = self.matrix
        return (m[0][0] * p[0] + m[0][1] * p[1] + m[0][2], m[1][0] * p[0] + m[1][1] * p[1] + m[1][2])

    def to_json(self) -> Dict[str, object]:
        d = asdict(self)
        # Inverse for px -> mm and the legacy Alignment fields.
        A = np.array(self.matrix + [[0.0, 0.0, 1.0]])
        inv = np.linalg.inv(A)
        d["px_to_mm"] = inv[:2].tolist()
        d["mm_per_px"] = 1.0 / self.px_per_mm if self.px_per_mm else None
        d["mm_to_px"] = self.matrix
        d["match"] = self.match
        return d


# ------------------------------------------------------------- image side
@dataclass
class ImageEvidence:
    path: str
    size: Tuple[int, int]  # original width, height
    scale: float  # refine image px per original px
    lines: np.ndarray  # bool, long straight dark strokes at refine resolution
    dist: np.ndarray  # float32 distance to nearest line pixel (refine px)
    nearest: Tuple[np.ndarray, np.ndarray]  # row, col of nearest line pixel
    coarse_scale: float
    coarse: np.ndarray  # float32 line map at coarse resolution (0..1)
    coarse_blur: np.ndarray  # float32 blurred line map at coarse resolution


def _nearest_dark(dark: np.ndarray) -> Tuple[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
    try:
        from scipy import ndimage

        dist, idx = ndimage.distance_transform_edt(~dark, return_indices=True)
        return dist.astype(np.float32), (idx[0], idx[1])
    except ImportError:  # pragma: no cover - fallback path
        src = (~dark).astype(np.uint8)
        dist, labels = cv2.distanceTransformWithLabels(src, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
        rows, cols = np.nonzero(dark)
        lut_r = np.zeros(labels.max() + 1, np.int64)
        lut_c = np.zeros(labels.max() + 1, np.int64)
        lab = labels[rows, cols]
        lut_r[lab], lut_c[lab] = rows, cols
        return dist.astype(np.float32), (lut_r[labels], lut_c[labels])


def texture_mask(dark: np.ndarray, close_px: int = 5, open_px: int = 15) -> np.ndarray:
    """Hatched / tiled areas (terraces, floor patterns, legends).

    Closing turns hatching into solid blobs; opening with a kernel wider
    than any thin wall keeps only those blobs. Blobs that are (nearly) solid
    in the original are thick solid-black walls, not hatching, and are kept.
    """
    m = dark.astype(np.uint8)
    blobs = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((close_px, close_px), np.uint8))
    blobs = cv2.morphologyEx(blobs, cv2.MORPH_OPEN, np.ones((open_px, open_px), np.uint8)).astype(bool)
    density = cv2.blur(dark.astype(np.float32), (open_px, open_px))
    hatch = blobs & (density < 0.75)
    hatch = cv2.dilate(hatch.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    return hatch & ~(density >= 0.9)


def long_lines(dark: np.ndarray, length: int = 21) -> np.ndarray:
    """Dark strokes that run straight for ``length`` px horizontally or vertically.

    Walls survive (solid or double-line); furniture, fixtures, text and
    hatching mostly do not.
    """
    m = dark.astype(np.uint8)
    h = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((1, length), np.uint8))
    v = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((length, 1), np.uint8))
    return (h | v).astype(bool)


def load_image(path: str | Path) -> ImageEvidence:
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(path)).convert("L")
    w, h = img.size
    s = REFINE_SIDE / max(w, h)
    gray = np.asarray(img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR), np.uint8)
    # Dark = clearly darker than paper; Otsu but never above 200.
    thr, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = gray < min(float(thr), 200.0)
    dark &= ~texture_mask(dark)
    lines = long_lines(dark)
    if lines.sum() < 200:  # nothing straight: fall back to all dark pixels
        lines = dark
    dist, nearest = _nearest_dark(lines)
    cs = COARSE_SIDE / max(w, h)
    small = cv2.resize(lines.astype(np.float32), (max(1, round(w * cs)), max(1, round(h * cs))),
                       interpolation=cv2.INTER_AREA)
    small = np.clip(small * 3.0, 0, 1).astype(np.float32)
    blur = cv2.GaussianBlur(small, (3, 3), 0).astype(np.float32)
    return ImageEvidence(str(path), (w, h), s, lines, dist, nearest, cs, small, blur)


def load_image_edges(path: str | Path) -> ImageEvidence:
    """Like ``load_image`` but the evidence is long Canny edges of the grey
    image instead of dark strokes. Coloured and photo-style brochure plans
    draw walls as colour boundaries, not black lines, so the dark-stroke
    evidence locks onto furniture and picks a wrong scale there."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(path)).convert("L")
    w, h = img.size
    s = REFINE_SIDE / max(w, h)
    gray = np.asarray(img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.BILINEAR), np.uint8)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 40, 120) > 0
    edges = cv2.dilate(edges.astype(np.uint8), np.ones((2, 2), np.uint8)) > 0
    lines = long_lines(edges, 31)
    if lines.sum() < 200:
        lines = edges
    dist, nearest = _nearest_dark(lines)
    cs = COARSE_SIDE / max(w, h)
    small = cv2.resize(lines.astype(np.float32), (max(1, round(w * cs)), max(1, round(h * cs))),
                       interpolation=cv2.INTER_AREA)
    small = np.clip(small * 3.0, 0, 1).astype(np.float32)
    blur = cv2.GaussianBlur(small, (3, 3), 0).astype(np.float32)
    return ImageEvidence(str(path), (w, h), s, lines, dist, nearest, cs, small, blur)


# ------------------------------------------------------------- model side
def model_points(gt: StoreyGT, step: float = 40.0) -> np.ndarray:
    """Points (mm) along wall/railing outlines, minus the stretches inside openings."""
    openings = [o.polygon for o in gt.openings]
    pts: List[Point] = []
    for kind, poly in gt.wall_polygons:
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            L = math.dist(a, b)
            k = max(1, int(L / step))
            for j in range(k):
                t = (j + 0.5) / k
                q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                if any(point_in_polygon(q, op) for op in openings):
                    continue
                pts.append(q)
    return np.array(pts, float).reshape(-1, 2)


def model_edges(gt: StoreyGT, min_length: float = 400.0, step: float = 40.0) -> List[np.ndarray]:
    """Samples (mm) along each wall/railing outline edge of at least
    ``min_length``, minus opening stretches; one array per edge."""
    openings = [o.polygon for o in gt.openings]
    out: List[np.ndarray] = []
    for _, poly in gt.wall_polygons:
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            L = math.dist(a, b)
            if L < min_length:
                continue
            k = max(2, int(L / step))
            qs = []
            for j in range(k):
                t = (j + 0.5) / k
                q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                if not any(point_in_polygon(q, op) for op in openings):
                    qs.append(q)
            if len(qs) >= 3:
                out.append(np.array(qs, float))
    return out


@dataclass
class StoreyModel:
    """Per-storey sample sets, prepared once and reused for every image."""

    gt: StoreyGT
    points: np.ndarray
    edges: List[np.ndarray]


def prepare_model(gt: StoreyGT) -> StoreyModel:
    return StoreyModel(gt, model_points(gt), model_edges(gt))


def edge_support(ev: "ImageEvidence", edges: Sequence[np.ndarray], A: np.ndarray,
                 need: float = 0.6, tol: float = 3.0) -> float:
    """Share of wall faces drawn along at least ``need`` of their length.

    A true registration puts whole wall faces on drawn lines; a false one
    only touches scattered pieces of many faces.
    """
    if not edges:
        return 0.0
    B = _to_ref(ev, A)
    H, W = ev.dist.shape
    good = 0
    for e in edges:
        q = _apply(B, e)
        ix, iy = np.round(q[:, 0]).astype(int), np.round(q[:, 1]).astype(int)
        ok = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
        d = np.full(len(q), np.inf, np.float32)
        d[ok] = ev.dist[iy[ok], ix[ok]]
        good += float((d <= tol).mean()) >= need
    return good / len(edges)


def pose_matrix(rotation: int, mirror: bool) -> np.ndarray:
    """2x2 linear part mapping model (x right, y up) to image (x right, y down)."""
    flip = np.array([[1.0, 0.0], [0.0, -1.0]])  # y-up -> y-down
    if mirror:
        flip = np.array([[1.0, 0.0], [0.0, 1.0]])  # model y already points down the image
    r = math.radians(rotation)
    R = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
    return R @ flip


# ------------------------------------------------------------- coarse
def _coarse_candidates(ev: ImageEvidence, pts: np.ndarray, top: int = 3,
                       poses: Sequence[Tuple[int, bool]] = POSES) -> List[Tuple[float, np.ndarray, int, bool]]:
    """Best (score, 2x3 matrix in ORIGINAL px, rotation, mirror) over poses/scales.

    The score is the F-measure of precision (model points landing on line
    work) and recall (line work lying under the model). Recall is what stops
    a shrunken model from hiding inside dense furniture or text.
    """
    H, W = ev.coarse.shape
    long_side = max(W, H)
    total = float(ev.coarse.sum()) or 1.0
    dil = np.ones((3, 3), np.uint8)
    results: List[Tuple[float, np.ndarray, int, bool]] = []
    for rotation, mirror in poses:
        L = pose_matrix(rotation, mirror)
        q = pts @ L.T
        qmin, qmax = q.min(axis=0), q.max(axis=0)
        ext = float(max(qmax - qmin)) or 1.0
        best_pose = None
        for frac in np.exp(np.linspace(math.log(0.35), math.log(1.10), 26)):
            s = frac * long_side / ext  # coarse px per mm
            qq = (q - qmin) * s
            tw, th = int(math.ceil(qq[:, 0].max())) + 2, int(math.ceil(qq[:, 1].max())) + 2
            if tw >= W * 1.2 + 2 or th >= H * 1.2 + 2 or tw < 8 or th < 8:
                continue
            tmpl = np.zeros((th, tw), np.float32)
            ix = np.clip(np.round(qq[:, 0]).astype(int), 0, tw - 1)
            iy = np.clip(np.round(qq[:, 1]).astype(int), 0, th - 1)
            tmpl[iy, ix] = 1.0
            count = float(tmpl.sum()) or 1.0
            band = cv2.dilate(tmpl, dil)
            padx, pady = max(0, tw - W) + 4, max(0, th - H) + 4
            blur = cv2.copyMakeBorder(ev.coarse_blur, pady, pady, padx, padx, cv2.BORDER_CONSTANT, value=0)
            line = cv2.copyMakeBorder(ev.coarse, pady, pady, padx, padx, cv2.BORDER_CONSTANT, value=0)
            prec = cv2.matchTemplate(blur, tmpl, cv2.TM_CCORR) / count
            rec = cv2.matchTemplate(line, band, cv2.TM_CCORR) / total
            f = 2 * prec * rec / np.maximum(prec + rec, 1e-6)
            _, val, _, loc = cv2.minMaxLoc(f)
            ox, oy = loc[0] - padx, loc[1] - pady
            if best_pose is None or val > best_pose[0]:
                A = np.zeros((2, 3))
                A[:, :2] = s * L
                A[:, 2] = -s * qmin + np.array([ox, oy])
                A /= ev.coarse_scale  # -> original px
                best_pose = (float(val), A, rotation, mirror)
        if best_pose is not None:
            results.append(best_pose)
    results.sort(key=lambda r: -r[0])
    return results[:top]


# ------------------------------------------------------------- refine
def _umeyama(src: np.ndarray, dst: np.ndarray, reflect: bool) -> np.ndarray:
    """Similarity (2x3) mapping src -> dst; keeps handedness ``reflect``."""
    mu_s, mu_d = src.mean(axis=0), dst.mean(axis=0)
    xs, xd = src - mu_s, dst - mu_d
    cov = xd.T @ xs / len(src)
    U, S, Vt = np.linalg.svd(cov)
    D = np.eye(2)
    want = -1.0 if reflect else 1.0
    if np.sign(np.linalg.det(U @ Vt)) != want:
        D[1, 1] = -1.0
    R = U @ D @ Vt
    var = (xs ** 2).sum() / len(src)
    s = float(np.trace(np.diag(S) @ D) / var) if var > 0 else 1.0
    A = np.zeros((2, 3))
    A[:, :2] = s * R
    A[:, 2] = mu_d - s * R @ mu_s
    return A


def _apply(A: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ A[:, :2].T + A[:, 2]


def _inlier(ev: ImageEvidence, A_ref: np.ndarray, pts: np.ndarray, tol: float = 3.0) -> float:
    q = _apply(A_ref, pts)
    H, W = ev.dist.shape
    ix, iy = np.round(q[:, 0]).astype(int), np.round(q[:, 1]).astype(int)
    ok = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
    if not len(q):
        return 0.0
    d = np.full(len(q), np.inf, np.float32)
    d[ok] = ev.dist[iy[ok], ix[ok]]
    return float((d <= tol).mean())


def refine(ev: ImageEvidence, pts: np.ndarray, A0: np.ndarray) -> np.ndarray:
    """Trimmed ICP to the nearest dark pixel; returns 2x3 in original px."""
    S = np.diag([ev.scale, ev.scale])
    A = A0.copy()
    A[:, :2] = S @ A[:, :2]
    A[:, 2] = S @ A[:, 2]
    reflect = np.linalg.det(A[:, :2]) < 0
    H, W = ev.dist.shape
    nr, nc = ev.nearest
    for thr in (14.0, 9.0, 6.0, 4.0, 3.0):
        for _ in range(6):
            q = _apply(A, pts)
            ix, iy = np.round(q[:, 0]).astype(int), np.round(q[:, 1]).astype(int)
            ok = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
            if ok.sum() < 20:
                break
            d = ev.dist[iy[ok], ix[ok]]
            keep = d <= thr
            if keep.sum() < 20:
                break
            src = pts[ok][keep]
            dst = np.stack([nc[iy[ok], ix[ok]][keep], nr[iy[ok], ix[ok]][keep]], axis=1).astype(float)
            A_new = _umeyama(src, dst, reflect)
            # keep near the coarse pose: small rotation only
            if abs(math.degrees(math.atan2(A_new[1, 0], A_new[0, 0]) - math.atan2(A[1, 0], A[0, 0]))) > 5:
                break
            if np.abs(A_new - A).max() < 1e-3:
                A = A_new
                break
            A = A_new
    A_orig = A.copy()
    A_orig[:, :2] = A[:, :2] / ev.scale
    A_orig[:, 2] = A[:, 2] / ev.scale
    return A_orig


def _to_ref(ev: ImageEvidence, A: np.ndarray) -> np.ndarray:
    B = A.copy()
    B[:, :2] *= ev.scale
    B[:, 2] *= ev.scale
    return B


def _model_band(ev: ImageEvidence, B: np.ndarray, pts: np.ndarray, tol: int = 3) -> np.ndarray:
    H, W = ev.dist.shape
    q = _apply(B, pts)
    ix, iy = np.round(q[:, 0]).astype(int), np.round(q[:, 1]).astype(int)
    ok = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
    m = np.zeros((H, W), np.uint8)
    m[iy[ok], ix[ok]] = 1
    # model samples are ~40 mm apart; close the gaps then widen to the tolerance
    return cv2.dilate(m, np.ones((2 * tol + 3, 2 * tol + 3), np.uint8)).astype(bool)


def _fscore(ev: ImageEvidence, B: np.ndarray, pts: np.ndarray) -> Tuple[float, float, float]:
    p = _inlier(ev, B, pts)
    total = float(ev.lines.sum()) or 1.0
    r = float((ev.lines & _model_band(ev, B, pts)).sum()) / total
    return p, r, 2 * p * r / max(p + r, 1e-6)


def score_alignment(ev: ImageEvidence, pts: np.ndarray, A: np.ndarray) -> Tuple[float, float, float, float]:
    """(precision, recall, baseline F, normalised F) for a 2x3 in original px.

    Precision = model outline points within 3 px of a line pixel; recall =
    line pixels within ~3 px of the model outline. The baseline is the
    F-measure of the same model shifted 7% off-register, so busy drawings
    don't score well by chance.
    """
    B = _to_ref(ev, A)
    p, r, f = _fscore(ev, B, pts)
    H, W = ev.dist.shape
    base = []
    for dx, dy in [(0.07 * W, 0), (-0.07 * W, 0), (0, 0.07 * H), (0, -0.07 * H)]:
        C = B.copy()
        C[:, 2] += (dx, dy)
        base.append(_fscore(ev, C, pts)[2])
    b = float(np.mean(base))
    return p, r, b, (f - b) / max(1e-6, 1.0 - b)


def register(ev: ImageEvidence, gt: StoreyGT, model: Optional[StoreyModel] = None,
             top: int = 3, poses: Sequence[Tuple[int, bool]] = POSES) -> Optional[Registration]:
    """Best registration of one storey onto one image (None if no walls)."""
    model = prepare_model(gt) if model is None else model
    pts = model.points
    if len(pts) < 30:
        return None
    best: Optional[Registration] = None
    for _, A0, rotation, mirror in _coarse_candidates(ev, pts, top=min(top, len(poses)), poses=poses):
        A = refine(ev, pts, A0)
        prec, rec, base, sc = score_alignment(ev, pts, A)
        sup = edge_support(ev, model.edges, A)
        px_per_mm = float(math.sqrt(abs(np.linalg.det(A[:, :2]))))
        reg = Registration(ev.path, gt.key, ev.size, A.tolist(), rotation, mirror, px_per_mm, prec, base, sc,
                           recall=rec, support=sup)
        if best is None or reg.match > best.match:
            best = reg
    return best


# ------------------------------------------------------------- matching
def _assign(score: np.ndarray, none_score: float = 0.0) -> List[Tuple[int, int]]:
    """1:1 assignment maximising total score; an image may stay unassigned
    (no storey scores above ``none_score``), so unrelated pictures in a
    folder are not forced onto a storey."""
    n, m = score.shape
    padded = np.concatenate([score, np.full((n, n), none_score)], axis=1)
    try:
        from scipy.optimize import linear_sum_assignment

        r, c = linear_sum_assignment(-padded)
        return [(i, j) for i, j in zip(r.tolist(), c.tolist()) if j < m]
    except ImportError:  # pragma: no cover - greedy fallback
        pairs, used_r, used_c = [], set(), set()
        for flat in np.argsort(-score, axis=None):
            i, j = divmod(int(flat), score.shape[1])
            if score[i, j] <= none_score:
                break
            if i not in used_r and j not in used_c:
                pairs.append((i, j))
                used_r.add(i)
                used_c.add(j)
        return pairs


def bucket(reg: Registration, ok_support: float = 0.55, ok_score: float = 0.18,
           review_match: float = 0.30, min_margin: float = 0.08,
           review_score: float = 0.20) -> str:
    """Precision-first thresholds, calibrated on hand-checked pairs.

    Correct registrations: support 0.42-0.80, score 0.18-0.38.
    Wrong storey / unrelated image: support 0.20-0.50, score 0.09-0.32.
    ``auto_ok`` must never admit a wrong pair; borderline correct pairs go
    to ``review``.
    """
    if "not_line_drawing" in reg.notes:
        return "reject"
    if ("unusual_pose" not in reg.notes and reg.support >= ok_support and reg.score >= ok_score
            and reg.margin >= min_margin):
        return "auto_ok"
    # Below this score the fit is indistinguishable from a wrong storey (folders
    # 10-14 hold unrelated plans: every review pair there scored 0.09-0.20).
    if reg.match >= review_match and reg.score >= review_score:
        return "review"
    return "reject"


def match_folder(images: Sequence[str | Path], storeys: Sequence[StoreyGT], log=print,
                 none_score: float = 0.28, poses: Sequence[Tuple[int, bool]] = IMAGE_FRAME_POSES,
                 fallback_all_poses: bool = True) -> Tuple[List[Registration], np.ndarray]:
    """Register every image against every storey and assign 1:1.

    The first pass tries only ``poses`` (default: the image-frame pose plan
    tracers export in); images with no storey above ``none_score`` are then
    retried over all 8 poses when ``fallback_all_poses``. Images still below
    ``none_score`` stay unassigned. Returns the chosen registrations and the
    combined match matrix (images x storeys).
    """
    models = [prepare_model(gt) for gt in storeys]
    regs: List[List[Optional[Registration]]] = []
    score = np.full((len(images), len(storeys)), -1.0)
    for i, path in enumerate(images):
        ev = load_image(path)
        density = float(ev.lines.mean())
        row: List[Optional[Registration]] = []

        def run(pose_set):
            row.clear()
            for j, gt in enumerate(storeys):
                reg = register(ev, gt, models[j], top=2, poses=pose_set)
                if reg is not None and density > MAX_LINE_DENSITY:
                    reg.notes.append("not_line_drawing")
                row.append(reg)
                score[i, j] = reg.match if reg is not None else -1.0

        run(poses)
        if fallback_all_poses and len(storeys) and score[i].max() < none_score and list(poses) != POSES:
            run(POSES)
        if density > MAX_LINE_DENSITY:
            score[i] = np.minimum(score[i], 0.0)  # renders/photos never get assigned
        regs.append(list(row))
        if log and len(storeys):
            best = int(np.argmax(score[i]))
            log(f"  {Path(path).name}: best {storeys[best].key} match {score[i].max():.2f}"
                + ("  (not a line drawing)" if density > MAX_LINE_DENSITY else ""))
    chosen: List[Registration] = []
    for i, j in _assign(score, none_score=none_score):
        reg = regs[i][j]
        if reg is None:
            continue
        twins = [k for k in range(len(storeys)) if k != j and _same_storey(storeys[k], storeys[j])]
        rest = np.delete(score[i], [j] + twins)
        reg.margin = float(reg.match - (rest.max() if len(rest) else 0.0))
        if (reg.rotation, reg.mirror) not in list(poses):
            reg.notes.append("unusual_pose")
        reg.bucket = bucket(reg)
        chosen.append(reg)
    return chosen, score


def _same_storey(a: StoreyGT, b: StoreyGT, tol: float = 50.0) -> bool:
    if a.stats.get("walls") != b.stats.get("walls"):
        return False
    ba, bb = a.bounds(), b.bounds()
    return all(abs(x - y) <= tol for x, y in zip(ba, bb))


def save_alignment(reg: Registration, path: str | Path) -> None:
    Path(path).write_text(json.dumps(reg.to_json(), indent=1), encoding="utf-8")


def render_overlay(reg: Registration, gt: StoreyGT, out_path: str | Path, max_side: int = 1400) -> None:
    """Draw the registered storey (walls, openings, room names) over the image."""
    from PIL import Image, ImageDraw, ImageOps

    img = ImageOps.exif_transpose(Image.open(reg.image)).convert("RGB")
    k = min(1.0, max_side / max(img.size))
    if k < 1.0:
        img = img.resize((round(img.width * k), round(img.height * k)), Image.BILINEAR)
    base = img.copy()
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    px = lambda p: tuple(c * k for c in reg.to_px(p))  # noqa: E731
    for kind, poly in gt.wall_polygons:
        d.polygon([px(p) for p in poly], fill=(255, 0, 0, 70) if kind == "wall" else (255, 140, 0, 70),
                  outline=(220, 0, 0, 255) if kind == "wall" else (255, 140, 0, 255))
    for o in gt.openings:
        d.polygon([px(p) for p in o.polygon], fill=(0, 120, 255, 110) if o.kind == "window" else (0, 200, 0, 110))
    for sp in gt.spaces:
        x, y = px(sp.seed)
        d.text((x - 20, y - 6), sp.name[:16], fill=(160, 0, 160, 255))
    img = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    ImageDraw.Draw(img).text((8, 8), f"{Path(reg.storey).name}  support {reg.support:.2f}  score {reg.score:.2f}  "
                             f"margin {reg.margin:.2f}  {reg.bucket}", fill=(0, 0, 0))
    img.save(out_path)
