"""Fine-tune CubiCasa5K on your own traced-and-corrected plans.

Data: folders written by the ``export_training_example`` MCP tool, each with
image.png, rooms.png (room/wall classes) and icons.png (window/door classes),
by default in Documents/TopoSpatial Exports/dataset.

Output: models/cubicasa_finetuned.pth. The tracer's neural engine prefers it
over the original weights automatically (restart Claude after training).

    .venv\\Scripts\\python.exe scripts\\finetune_cubicasa.py --smoke
    .venv\\Scripts\\python.exe scripts\\finetune_cubicasa.py --epochs 60

Thin-wall weighting (default on, ``--thin-wall-weight 3``): wall pixels of
walls up to ``--thin-wall-mm`` thick (250 mm: the IFC labels draw every
internal partition at 200 mm and external walls at 350 mm; partitions between toilets,
dressing rooms, bedrooms) count ``--thin-wall-weight`` times in the wall loss.
Without it the few pixels of a thin partition are swamped by thick outer
walls, the model learns to skip them, and adjacent rooms merge. Validation
also reports ``thin_wall_recall``. ``--thin-wall-weight 1`` restores the old loss.

Junction heatmaps (``--heatmap-weight``, default 0 = off): the network also
outputs 21 heatmaps that the plain fine-tune leaves untrained, so they drift
(measured 10 Nov 2026: correct-type T-junction recall 0.84 pretrained -> 0.75
after fine-tuning, background noise 5x higher). With a weight > 0, samples
that have a graph.json are also trained on channels 0-12 (wall junctions by
shape and direction) and 13-16 (opening ends: left, right, up, down). Channel
layout, peak height (1.0) and width (sigma 6 px) were read off the pretrained
weights with scripts/junction_probe.py. Icon-corner channels 17-20 and samples
without a graph are left out of this loss. Validation then also reports
``junction_recall`` / ``junction_precision`` / ``opening_end_recall``.

Needs a CUDA build of PyTorch for real training. ``--smoke`` runs two tiny
steps on any device to check the setup. CubiCasa5K weights are CC BY-NC 4.0
(non-commercial); a model fine-tuned from them carries the same licence.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from PIL import Image  # noqa: E402

from trace_engine._cubicasa_model import hg_furukawa_original  # noqa: E402
from trace_engine.neural import ICON_CLASSES, ROOM_CLASSES  # noqa: E402

SPLIT = (21, 12, 11)
IGNORE = 255
WALL, WINDOW, DOOR = ROOM_CLASSES.index("Wall"), ICON_CLASSES.index("Window"), ICON_CLASSES.index("Door")


# --- junction heatmaps -------------------------------------------------------
# Arm bits in the image frame (x right, y down).
ARM_R, ARM_D, ARM_L, ARM_U = 1, 2, 4, 8
# Wall-junction channel by arm set (FloorNet / CubiCasa order, confirmed on the
# pretrained weights): 0-3 wall ends, 4-7 corners, 8-11 T junctions, 12 cross.
JUNCTION_CHANNEL = {
    ARM_D: 0, ARM_L: 1, ARM_U: 2, ARM_R: 3,
    ARM_U | ARM_L: 4, ARM_U | ARM_R: 5, ARM_R | ARM_D: 6, ARM_D | ARM_L: 7,
    ARM_R | ARM_D | ARM_L: 8, ARM_U | ARM_D | ARM_L: 9, ARM_U | ARM_R | ARM_L: 10, ARM_U | ARM_R | ARM_D: 11,
    ARM_R | ARM_D | ARM_L | ARM_U: 12,
}
# Opening-end channel by the direction towards the opening's other end:
# 13 = left end, 14 = right end, 15 = upper end, 16 = lower end.
OPENING_CHANNEL = {ARM_R: 13, ARM_L: 14, ARM_D: 15, ARM_U: 16}
HEAT_CHANNELS = 17  # trained channels (0-16); icon corners 17-20 have no labels
PT_JUNCTION, PT_OPENING, PT_IGNORE = 0.0, 1.0, 2.0
_HFLIP = {ARM_R: ARM_L, ARM_L: ARM_R, ARM_U: ARM_U, ARM_D: ARM_D}
_VFLIP = {ARM_U: ARM_D, ARM_D: ARM_U, ARM_R: ARM_R, ARM_L: ARM_L}
_ROT90 = {ARM_R: ARM_U, ARM_U: ARM_L, ARM_L: ARM_D, ARM_D: ARM_R}  # np.rot90: counter-clockwise on screen


def _arm_bit(deg: float):
    a = deg % 360.0
    k = int(round(a / 90.0)) % 4
    return (ARM_R, ARM_D, ARM_L, ARM_U)[k], abs(((a - 90.0 * k) + 180.0) % 360.0 - 180.0)


def _remap(mask: int, table: dict) -> int:
    out = 0
    for bit, to in table.items():
        if mask & bit:
            out |= to
    return out


def load_points(folder: Path, axis_tol: float = 20.0):
    """Junction labels of one sample as rows (x, y, kind, arm mask), or None.

    Wall junctions use wall edges only (separators bound rooms but are not
    walls). Corners, T and X junctions on the image axes are targets. Wall
    ends, skewed junctions and doubled arms become ignore points: their labels
    are unreliable, so no loss is taken around them. Straight-through nodes
    are not junctions and stay background."""
    path = folder / "graph.json"
    if not path.is_file():
        return None
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))["graph_px"]
    except (ValueError, KeyError):
        return None
    pos = {n["id"]: (float(n["x"]), float(n["y"])) for n in graph.get("nodes", [])}
    arms = {k: [] for k in pos}
    for e in graph.get("edges", []):
        a, b = e.get("source"), e.get("target")
        if e.get("type") == "separator" or a not in pos or b not in pos:
            continue
        (ax, ay), (bx, by) = pos[a], pos[b]
        arms[a].append(math.degrees(math.atan2(by - ay, bx - ax)))
        arms[b].append(math.degrees(math.atan2(ay - by, ax - bx)))
    rows = []
    for k, (x, y) in pos.items():
        if not arms[k]:
            continue
        mask, ok = 0, True
        for d in arms[k]:
            bit, dev = _arm_bit(d)
            ok = ok and dev <= axis_tol and not (mask & bit)
            mask |= bit
        if ok and mask in (ARM_R | ARM_L, ARM_U | ARM_D):
            continue  # straight through: no junction
        target = ok and mask in JUNCTION_CHANNEL and len(arms[k]) >= 2
        rows.append((x, y, PT_JUNCTION if target else PT_IGNORE, float(mask)))
    for o in graph.get("openings", []):
        ends = o.get("ends_px") or []
        if len(ends) != 2:
            continue
        for j in (0, 1):
            (x, y), (ox, oy) = ends[j], ends[1 - j]
            bit, dev = _arm_bit(math.degrees(math.atan2(oy - y, ox - x)))
            rows.append((float(x), float(y), PT_OPENING if dev <= axis_tol else PT_IGNORE, float(bit)))
    return np.asarray(rows, np.float32).reshape(-1, 4)


def transform_points(pts, sx, sy, x0, y0, size, hflip, vflip, k):
    """The same scale, crop, flips and quarter turns as ``augment`` applies to the image."""
    if pts is None:
        return None
    out = pts.copy()
    out[:, 0] = out[:, 0] * sx - x0
    out[:, 1] = out[:, 1] * sy - y0
    masks = [int(m) for m in out[:, 3]]
    if hflip:
        out[:, 0] = size - 1 - out[:, 0]
        masks = [_remap(m, _HFLIP) for m in masks]
    if vflip:
        out[:, 1] = size - 1 - out[:, 1]
        masks = [_remap(m, _VFLIP) for m in masks]
    for _ in range(k % 4):
        out[:, 0], out[:, 1] = out[:, 1].copy(), size - 1 - out[:, 0].copy()
        masks = [_remap(m, _ROT90) for m in masks]
    out[:, 3] = masks
    return out


def render_heat(pts, height, width, sigma):
    """(target, valid) for channels 0-16: Gaussian peaks of height 1, and a
    validity mask that is 0 around ignore points."""
    target = np.zeros((HEAT_CHANNELS, height, width), np.float32)
    valid = np.ones((HEAT_CHANNELS, height, width), np.float32)
    if pts is None or not len(pts):
        return target, valid
    r = int(math.ceil(3 * sigma))
    for x, y, kind, mask in pts:
        cx, cy = int(round(float(x))), int(round(float(y)))
        x0, x1, y0, y1 = max(0, cx - r), min(width, cx + r + 1), max(0, cy - r), min(height, cy + r + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        if kind == PT_IGNORE:
            valid[:, y0:y1, x0:x1] = 0.0
            continue
        ch = (JUNCTION_CHANNEL if kind == PT_JUNCTION else OPENING_CHANNEL).get(int(mask))
        if ch is None:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        g = np.exp(-((xx - float(x)) ** 2 + (yy - float(y)) ** 2) / (2.0 * sigma * sigma)).astype(np.float32)
        np.maximum(target[ch, y0:y1, x0:x1], g, out=target[ch, y0:y1, x0:x1])
    return target, valid


def heatmap_loss(pred, target, valid):
    """Squared error over valid pixels, normalised by the amount of peak in the
    batch (so it reads as error per peak pixel and stays comparable between
    crops with many and few junctions). Background errors count too."""
    err = (pred - target) ** 2 * valid
    return err.sum() / ((target * valid).sum() + 100.0)


def build_model(weights: Path) -> torch.nn.Module:
    model = hg_furukawa_original(n_classes=51)
    n = sum(SPLIT)
    model.conv4_ = torch.nn.Conv2d(256, n, bias=True, kernel_size=1)
    model.upsample = torch.nn.ConvTranspose2d(n, n, kernel_size=4, stride=4)
    try:
        ckpt = torch.load(str(weights), map_location="cpu", weights_only=True)
    except Exception:
        ckpt = torch.load(str(weights), map_location="cpu", weights_only=False)
    model.load_state_dict(ckpt.get("model_state", ckpt))
    return model


def thin_wall_weights(rooms: np.ndarray, px_per_mm, thin_mm: float, weight: float) -> np.ndarray:
    """Per-pixel loss weight: ``weight`` on wall pixels of thin walls, 1 elsewhere.

    Local wall thickness = 2 x the largest distance-to-background found within
    the wall nearby (a max filter spreads each wall's half-width over its
    pixels). Without a scale, "thin" means thinner than 0.6 x the image's
    median wall thickness.
    """
    out = np.ones(rooms.shape, np.float32)
    wall = rooms == WALL
    if weight == 1.0 or not wall.any():
        return out
    from scipy import ndimage

    half = ndimage.distance_transform_edt(wall)
    if px_per_mm:
        limit_px = thin_mm * float(px_per_mm)
    else:
        ridge = half[(half > 0) & (half >= ndimage.maximum_filter(half, size=3))]
        limit_px = 0.6 * 2.0 * float(np.median(ridge)) if ridge.size else 0.0
    if limit_px <= 0:
        return out
    size = max(3, int(round(limit_px)) | 1)
    local = 2.0 * ndimage.maximum_filter(half, size=size)
    out[wall & (local <= limit_px)] = weight
    return out


def load_examples(root: Path, require_reviewed=False, thin_mm: float = 150.0, thin_weight: float = 1.0):
    if not root.exists():
        return []
    out = []
    for folder in sorted(p for p in root.iterdir() if (p / "rooms.png").is_file()):
        meta_path = folder / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        if require_reviewed:
            if meta.get("annotation_complete") is not True:
                raise ValueError(f"{folder}: review labels and mark meta.json annotation_complete=true; unknown pixels must be 255 or covered by ignore.png")
        img = np.asarray(Image.open(folder / "image.png").convert("RGB"), np.uint8)
        rooms = np.array(Image.open(folder / "rooms.png"))
        icons = np.array(Image.open(folder / "icons.png"))
        for label, mask, classes in (("rooms", rooms, SPLIT[1]), ("icons", icons, SPLIT[2])):
            if mask.ndim != 2 or mask.shape != img.shape[:2]:
                raise ValueError(f"{folder}: {label} must be a single-channel mask matching image dimensions")
            if not np.isin(mask, list(range(classes)) + [IGNORE]).all():
                raise ValueError(f"{folder}: invalid {label} class IDs")
        rooms, icons = rooms.astype(np.uint8), icons.astype(np.uint8)
        ignore_path = folder / "ignore.png"
        if ignore_path.exists():
            ignore = np.asarray(Image.open(ignore_path))
            if ignore.ndim != 2 or ignore.shape != rooms.shape:
                raise ValueError(f"{folder}: ignore.png must match mask dimensions")
            rooms[ignore != 0] = IGNORE
            icons[ignore != 0] = IGNORE
        ppm = (meta.get("registration") or {}).get("px_per_mm")
        out.append((folder.name, img, rooms, icons, thin_wall_weights(rooms, ppm, thin_mm, thin_weight),
                    load_points(folder)))
    return out


def augment(img, rooms, icons, wmap, pts, crop):
    """Random scale, crop, flips and 90-degree turns (labels, the pixel weight
    map and the junction points stay aligned). Returns the points transformed,
    or None when the sample has none."""
    s = random.uniform(0.75, 1.4)
    h, w = rooms.shape
    size = (max(8, int(w * s)), max(8, int(h * s)))
    sx, sy = size[0] / float(w), size[1] / float(h)
    img = np.asarray(Image.fromarray(img).resize(size, Image.BILINEAR))
    rooms = np.asarray(Image.fromarray(rooms).resize(size, Image.NEAREST))
    icons = np.asarray(Image.fromarray(icons).resize(size, Image.NEAREST))
    wmap = np.asarray(Image.fromarray(wmap).resize(size, Image.NEAREST))
    h, w = rooms.shape
    # Prefer crops that contain walls so blank margins don't dominate.
    for _ in range(10):
        y = random.randint(0, max(0, h - crop))
        x = random.randint(0, max(0, w - crop))
        if (rooms[y:y + crop, x:x + crop] == WALL).mean() > 0.02:
            break
    img, rooms, icons = img[y:y + crop, x:x + crop], rooms[y:y + crop, x:x + crop], icons[y:y + crop, x:x + crop]
    wmap = wmap[y:y + crop, x:x + crop]
    ph, pw = crop - rooms.shape[0], crop - rooms.shape[1]
    if ph or pw:
        img = np.pad(img, ((0, ph), (0, pw), (0, 0)), constant_values=255)
        rooms = np.pad(rooms, ((0, ph), (0, pw)), constant_values=IGNORE)
        icons = np.pad(icons, ((0, ph), (0, pw)), constant_values=IGNORE)
        wmap = np.pad(wmap, ((0, ph), (0, pw)), constant_values=1.0)
    hflip = random.random() < 0.5
    if hflip:
        img, rooms, icons, wmap = img[:, ::-1], rooms[:, ::-1], icons[:, ::-1], wmap[:, ::-1]
    vflip = random.random() < 0.5
    if vflip:
        img, rooms, icons, wmap = img[::-1], rooms[::-1], icons[::-1], wmap[::-1]
    k = random.randint(0, 3)
    img, rooms, icons, wmap = np.rot90(img, k), np.rot90(rooms, k), np.rot90(icons, k), np.rot90(wmap, k)
    return (np.ascontiguousarray(img), np.ascontiguousarray(rooms), np.ascontiguousarray(icons),
            np.ascontiguousarray(wmap), transform_points(pts, sx, sy, x, y, crop, hflip, vflip, k))


def partition_examples(examples, root, val_fraction):
    """Honor annotation-studio assignments; reject building leakage."""
    metadata = {}
    for ex in examples:
        path = Path(root) / ex[0] / "meta.json"
        metadata[ex[0]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if any("split" in meta for meta in metadata.values()):
        groups = {}
        train, val = [], []
        for ex in examples:
            meta = metadata[ex[0]]
            split = meta.get("split")
            if split not in ("train", "val"):
                raise ValueError("Every example must have a train/val assignment when using explicit splits")
            for identity in ("group:" + meta.get("group_id", ex[0]), "hash:" + meta.get("source_sha256", ex[0])):
                if identity in groups and groups[identity] != split:
                    raise ValueError("A building/group or duplicate source occurs in both train and validation")
                groups[identity] = split
            (train if split == "train" else val).append(ex)
        if not train:
            raise ValueError("Assign at least one reviewed example to training")
        return train, val
    n_val = min(len(examples) - 1, max(1, int(round(len(examples) * val_fraction)))) if len(examples) > 1 and val_fraction > 0 else 0
    return examples[n_val:], examples[:n_val]


def to_tensor(img):
    t = torch.from_numpy(img.transpose(2, 0, 1).astype(np.float32))
    return 2.0 * (t / 255.0) - 1.0


def class_weights(examples, idx, n):
    counts = np.ones(n)
    for ex in examples:
        counts += np.bincount(ex[idx].ravel(), minlength=n)[:n]
    w = 1.0 / np.sqrt(counts / counts.sum())
    return torch.tensor(w / w.mean(), dtype=torch.float32)


def masked_cross_entropy(logits, target, weights, pixel_weights=None):
    """Keep a task with no annotated pixels finite and gradient-neutral.

    With ``pixel_weights`` each pixel's loss is also multiplied by its weight;
    normalised like PyTorch's class-weighted mean (sum of the weights used)."""
    valid = target != IGNORE
    if not valid.any():
        return logits.sum() * 0.0
    if pixel_weights is None:
        return F.cross_entropy(logits, target, weight=weights, ignore_index=IGNORE)
    per_px = F.cross_entropy(logits, target, weight=weights, ignore_index=IGNORE, reduction="none")
    w = weights[target.clamp(max=weights.numel() - 1)] * pixel_weights * valid
    return (per_px * pixel_weights).sum() / w.sum().clamp(min=1e-6)


@torch.no_grad()
def junction_scores(heat, pts, radius=8, threshold=0.3):
    """Counts for one plan: [junctions, found with the right channel, opening
    ends, found, wall-heat blobs, blobs near a labelled node]."""
    from scipy import ndimage

    h, w = heat.shape[1:]
    counts = [0, 0, 0, 0, 0, 0]
    for x, y, kind, mask in pts:
        if kind == PT_IGNORE:
            continue
        ch = (JUNCTION_CHANNEL if kind == PT_JUNCTION else OPENING_CHANNEL).get(int(mask))
        cx, cy = int(round(float(x))), int(round(float(y)))
        if ch is None or not (0 <= cx < w and 0 <= cy < h):
            continue
        win = heat[ch, max(0, cy - radius):cy + radius + 1, max(0, cx - radius):cx + radius + 1]
        i = 0 if kind == PT_JUNCTION else 2
        counts[i] += 1
        counts[i + 1] += int(win.size > 0 and float(win.max()) > threshold)
    blobs, n = ndimage.label(heat[:13].max(0) > threshold)
    if n:
        near = np.zeros((h, w), bool)
        for x, y, kind, mask in pts:
            if kind != PT_OPENING:
                cx, cy = int(round(float(x))), int(round(float(y)))
                near[max(0, cy - radius - 4):cy + radius + 5, max(0, cx - radius - 4):cx + radius + 5] = True
        counts[4] += n
        counts[5] += len(np.unique(blobs[near & (blobs > 0)]))
    return counts


@torch.no_grad()
def evaluate(model, examples, device, heatmaps=False):
    """IoU for wall / window / door on full images, plus the share of thin-wall
    pixels (weight map > 1) predicted as wall. With ``heatmaps`` also the share
    of labelled junctions / opening ends found by their own heatmap channel and
    the share of wall-junction blobs that sit on a labelled node."""
    was_training = model.training
    model.eval()
    inter = np.zeros(3)
    union = np.zeros(3)
    thin_hit = thin_all = 0
    jc = np.zeros(6)
    for _, img, rooms, icons, wmap, pts in examples:
        x = to_tensor(img)[None]
        h, w = rooms.shape
        x = F.pad(x, (0, (-w) % 64, 0, (-h) % 64), value=1.0).to(device)
        out = model(x)[0, :, :h, :w].float().cpu()
        r = out[SPLIT[0]:SPLIT[0] + SPLIT[1]].argmax(0).numpy()
        i = out[SPLIT[0] + SPLIT[1]:].argmax(0).numpy()
        for k, (pred, gt, valid) in enumerate(((r == WALL, rooms == WALL, rooms != IGNORE), (i == WINDOW, icons == WINDOW, icons != IGNORE), (i == DOOR, icons == DOOR, icons != IGNORE))):
            inter[k] += (pred & gt & valid).sum()
            union[k] += ((pred | gt) & valid).sum()
        thin = (wmap > 1.0) & (rooms == WALL)
        thin_all += int(thin.sum())
        thin_hit += int((thin & (r == WALL)).sum())
        if heatmaps and pts is not None and len(pts):
            jc += junction_scores(out[:HEAT_CHANNELS].numpy(), pts)
    model.train(was_training)
    iou = inter / np.maximum(union, 1)
    result = {name: float(iou[k]) if union[k] else None
              for k, name in enumerate(("wall", "window", "door"))}
    # Reported only; checkpoint selection still uses the three IoUs.
    result["thin_wall_recall"] = round(thin_hit / thin_all, 4) if thin_all else None
    if heatmaps:
        result["junction_recall"] = round(jc[1] / jc[0], 4) if jc[0] else None
        result["junction_precision"] = round(jc[5] / jc[4], 4) if jc[4] else None
        result["opening_end_recall"] = round(jc[3] / jc[2], 4) if jc[2] else None
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(REPO / "dataset" / "annotated_cubicasa"))
    ap.add_argument("--base", default=str(REPO / "models" / "pretrained" / "model_best_val_loss_var.pkl"),
                    help="starting weights (use models/cubicasa_finetuned.pth to continue training)")
    ap.add_argument("--out", default=str(REPO / "models" / "checkpoints" / "cubicasa_finetuned.pth"))
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--steps", type=int, default=0, help="steps per epoch (default: 4 x examples)")
    ap.add_argument("--crop", type=int, default=512)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--val", type=float, default=0.15, help="share of plans held out for validation")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--thin-wall-mm", type=float, default=250.0,
                    help="walls up to this thickness count as thin (IFC labels: internal 200, external 350 mm)")
    ap.add_argument("--thin-wall-weight", type=float, default=3.0,
                    help="loss weight on thin-wall pixels (1 = off)")
    ap.add_argument("--heatmap-weight", type=float, default=0.0,
                    help="weight of the junction-heatmap loss (0 = off, the old behaviour; try 1)")
    ap.add_argument("--heatmap-sigma", type=float, default=6.0,
                    help="width of the target peaks in px (6 = measured on the pretrained weights)")
    ap.add_argument("--smoke", action="store_true", help="2 tiny steps to check the setup; saves nothing")
    args = ap.parse_args()
    if args.epochs < 1 or args.batch < 1 or args.steps < 0 or args.lr <= 0:
        ap.error("epochs/batch/lr must be positive and steps nonnegative")
    if args.crop < 128 or args.crop % 64:
        ap.error("crop must be at least 128 and a multiple of 64")
    if not 0 <= args.val < 1:
        ap.error("val must be in [0, 1)")
    if args.thin_wall_weight < 1 or args.thin_wall_mm <= 0:
        ap.error("thin-wall-weight must be >= 1 and thin-wall-mm positive")
    if args.heatmap_weight < 0 or args.heatmap_sigma <= 0:
        ap.error("heatmap-weight must be >= 0 and heatmap-sigma positive")
    use_heat = args.heatmap_weight > 0

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device}" + (f" ({torch.cuda.get_device_name(0)})" if device.type == "cuda" else
                                 "  <- CPU: fine for --smoke, far too slow for real training"))
    try:
        examples = [] if args.smoke else load_examples(Path(args.data), require_reviewed=True,
                                                       thin_mm=args.thin_wall_mm, thin_weight=args.thin_wall_weight)
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    if not examples:
        if args.smoke:
            dummy_rooms = np.zeros((128, 128), np.uint8)
            dummy_rooms[40:60, :] = WALL
            dummy_icons = np.zeros((128, 128), np.uint8)
            dummy_icons[40:60, 20:40] = WINDOW
            dummy_rooms[80:84, :] = WALL  # a thin partition
            wmap = thin_wall_weights(dummy_rooms, None, args.thin_wall_mm, args.thin_wall_weight)
            smoke_pts = np.asarray([[30, 50, PT_JUNCTION, ARM_R | ARM_D], [90, 50, PT_JUNCTION, ARM_R | ARM_D | ARM_L],
                                    [20, 50, PT_OPENING, ARM_R], [40, 50, PT_OPENING, ARM_L],
                                    [100, 82, PT_IGNORE, ARM_R]], np.float32)
            examples = [("smoke", np.full((128, 128, 3), 255, np.uint8), dummy_rooms, dummy_icons, wmap, smoke_pts)]
        else:
            sys.exit(f"no examples in {args.data}; export some with export_training_example first")
    random.shuffle(examples)
    try:
        train, val = (examples, []) if args.smoke else partition_examples(examples, args.data, args.val)
    except ValueError as exc:
        ap.error(str(exc))
    if not any((ex[2] != IGNORE).any() or (ex[3] != IGNORE).any() for ex in train):
        ap.error("training partition has no annotated pixels")
    print(f"examples: {len(train)} train, {len(val)} validation")
    thin_px = sum(int((ex[4] > 1).sum()) for ex in train)
    wall_px = sum(int((ex[2] == WALL).sum()) for ex in train)
    print(f"thin-wall weighting: x{args.thin_wall_weight:g} on walls <= {args.thin_wall_mm:g} mm "
          f"({thin_px / max(wall_px, 1):.0%} of training wall pixels)")
    if use_heat:
        with_pts = [ex for ex in train if ex[5] is not None]
        n_j = sum(int((ex[5][:, 2] == PT_JUNCTION).sum()) for ex in with_pts)
        n_o = sum(int((ex[5][:, 2] == PT_OPENING).sum()) for ex in with_pts)
        n_i = sum(int((ex[5][:, 2] == PT_IGNORE).sum()) for ex in with_pts)
        print(f"junction heatmaps: weight {args.heatmap_weight:g}, sigma {args.heatmap_sigma:g} px; "
              f"{len(with_pts)} of {len(train)} training plans have a graph "
              f"({n_j} junctions, {n_o} opening ends, {n_i} ignored points)")
        if not with_pts:
            ap.error("--heatmap-weight needs samples with graph.json")
    if not val:
        print("No held-out validation: checkpoints indicate training progress only.")

    model = build_model(Path(args.base)).to(device)
    model.train()
    w_rooms = class_weights(train, 2, SPLIT[1]).to(device)
    w_icons = class_weights(train, 3, SPLIT[2]).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    epochs = 1 if args.smoke else args.epochs
    steps = 2 if args.smoke else (args.steps or max(20, 4 * len(train) // args.batch))
    crop = 128 if args.smoke else args.crop
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs * steps)
    use_bf16 = device.type == "cuda" and torch.cuda.is_bf16_supported()
    amp_dtype = torch.bfloat16 if use_bf16 else torch.float16
    scaler = torch.amp.GradScaler("cuda", enabled=device.type == "cuda" and not use_bf16)
    best = -1.0
    if val and not args.smoke:
        print("before fine-tuning:", evaluate(model, val, device, use_heat))

    for epoch in range(1, epochs + 1):
        t0, total, total_heat = time.time(), 0.0, 0.0
        for _ in range(steps):
            batch = [augment(*random.choice(train)[1:], crop) for _ in range(args.batch)]
            if use_heat:
                maps = [render_heat(b[4], crop, crop, args.heatmap_sigma) for b in batch]
                ht = torch.from_numpy(np.stack([m[0] for m in maps])).to(device)
                # Samples without a graph (CubiCasa mix) take no junction loss at all.
                hv = torch.from_numpy(np.stack([m[1] if b[4] is not None else np.zeros_like(m[1])
                                                for m, b in zip(maps, batch)]).astype(np.float32)).to(device)
            x = torch.stack([to_tensor(b[0]) for b in batch]).to(device)
            yr = torch.stack([torch.from_numpy(b[1].astype(np.int64)) for b in batch]).to(device)
            yi = torch.stack([torch.from_numpy(b[2].astype(np.int64)) for b in batch]).to(device)
            pw = torch.stack([torch.from_numpy(b[3].astype(np.float32)) for b in batch]).to(device)
            with torch.autocast(device_type=device.type, dtype=amp_dtype, enabled=device.type == "cuda"):
                out = model(x)
                rooms = out[:, SPLIT[0]:SPLIT[0] + SPLIT[1]].float()
                icons = out[:, SPLIT[0] + SPLIT[1]:].float()
                loss = (masked_cross_entropy(rooms, yr, w_rooms, pw if args.thin_wall_weight > 1 else None)
                        + masked_cross_entropy(icons, yi, w_icons))
                if use_heat:
                    # No junction loss where the labels are unknown (ignore mask, padding).
                    hv = hv * (yr != IGNORE).unsqueeze(1)
                    loss_heat = heatmap_loss(out[:, :HEAT_CHANNELS].float(), ht, hv)
                    loss = loss + args.heatmap_weight * loss_heat
            if not torch.isfinite(loss):
                raise RuntimeError("Non-finite training loss; checkpoint was not updated")
            opt.zero_grad(set_to_none=True)
            if scaler.is_enabled():
                scaler.scale(loss).backward()
                scaler.step(opt)
                scaler.update()
            else:
                loss.backward()
                opt.step()
            sched.step()
            total += float(loss.detach())
            if use_heat:
                total_heat += float(loss_heat.detach())
        line = f"epoch {epoch}/{epochs}  loss {total / steps:.4f}  {time.time() - t0:.0f}s"
        if use_heat:
            line += f"  heat {total_heat / steps:.4f}"
        if args.smoke:
            print(line + "  -> setup OK (nothing saved)")
            return
        metrics = evaluate(model, val, device, use_heat) if val else {}
        measured = [metrics[k] for k in ("wall", "window", "door") if metrics.get(k) is not None]
        score = float(np.mean(measured)) if measured else None
        print(line, metrics)
        if score is None or score > best:
            if score is not None:
                best = score
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            torch.save({"model_state": model.state_dict(), "epoch": epoch, "val": metrics,
                        "examples": len(train), "seed": args.seed,
                        "thin_wall": {"mm": args.thin_wall_mm, "weight": args.thin_wall_weight},
                        "heatmaps": {"weight": args.heatmap_weight, "sigma": args.heatmap_sigma},
                        "train_ids": [ex[0] for ex in train], "val_ids": [ex[0] for ex in val],
                        "split": SPLIT, "room_classes": ROOM_CLASSES, "icon_classes": ICON_CLASSES}, args.out)
            print(f"  saved {args.out}")
    print(json.dumps({"best_mean_iou": round(best, 3) if best >= 0 else None, "weights": args.out}))


if __name__ == "__main__":
    main()
