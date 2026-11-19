"""Optional neural wall/opening segmentation with CubiCasa5K.

The network labels every pixel as background / wall / room type and as
window / door / fixture. Its wall, window and door masks then go through the
same graph, room and opening logic as the classical tracer, so the output is
the same ``TraceResult``.

Needs PyTorch and the CubiCasa5K weights file
(``model_best_val_loss_var.pkl``). CubiCasa5K is CC BY-NC 4.0:
non-commercial use only.

Promoted settings: ``models/neural_profile.json`` names the weights, the wall
logit bias and the geometry pipeline that won on the frozen evaluation set
(``scripts/eval_trace.py``). When it exists, ``find_weights`` uses those
weights first and ``default_wall_bias`` returns its bias for them. Promote a
new model by editing that file after it beats the current one on the eval;
an explicit ``weights`` argument or ``TOPOSPATIAL_CUBICASA_WEIGHTS`` still wins.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

ROOM_CLASSES = ["Background", "Outdoor", "Wall", "Kitchen", "Living Room", "Bed Room",
                "Bath", "Entry", "Railing", "Storage", "Garage", "Undefined"]
ICON_CLASSES = ["No Icon", "Window", "Door", "Closet", "Electrical Appliance", "Toilet",
                "Sink", "Sauna Bench", "Fire Place", "Bathtub", "Chimney"]
WEIGHT_NAMES = ("cubicasa_finetuned.pth", "model_best_val_loss_var.pkl", "cubicasa5k.pkl", "cubicasa5k.pth")
_SPLIT = (21, 12, 11)
_MODEL_CACHE: Dict[str, object] = {}
REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = REPO_ROOT / "models" / "neural_profile.json"


def load_profile(path: Optional[Path] = None) -> Dict[str, Any]:
    """The promoted neural settings, or {} when there is no valid profile.

    ``weights`` may be relative to the repository root; it is returned
    resolved, and the profile is ignored when that file does not exist."""
    p = Path(path) if path else PROFILE_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data.get("weights"):
        return {}
    w = Path(str(data["weights"])).expanduser()
    if not w.is_absolute():
        w = REPO_ROOT / w
    if not w.is_file():
        logger.warning("neural profile %s names missing weights %s; ignored", p, w)
        return {}
    data["weights"] = str(w)
    try:
        data["wall_logit_bias"] = float(data.get("wall_logit_bias", 0.0))
    except (TypeError, ValueError):
        data["wall_logit_bias"] = 0.0
    return data


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or a.samefile(b)
    except OSError:
        return False


def default_wall_bias(weights: Optional[Path], profile: Optional[Dict[str, Any]] = None) -> float:
    """The promoted wall logit bias when ``weights`` are the promoted weights, else 0."""
    prof = load_profile() if profile is None else profile
    if weights is None or not prof:
        return 0.0
    return float(prof["wall_logit_bias"]) if _same_file(Path(weights), Path(prof["weights"])) else 0.0


def default_close_rooms(weights: Optional[Path], profile: Optional[Dict[str, Any]] = None) -> bool:
    """Whether the promoted profile turns on room closure from the raw masks
    (``"close_rooms": true``, see ``room_closure.py``) for these weights. Off
    for any other weights: it was measured with the promoted model only."""
    prof = load_profile() if profile is None else profile
    if weights is None or not prof or not prof.get("close_rooms"):
        return False
    return _same_file(Path(weights), Path(prof["weights"]))


def find_weights(explicit: Optional[str] = None, search_dirs: Iterable[str] = ()) -> Optional[Path]:
    """Locate the CubiCasa5K weights file."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    candidates = []
    env = os.environ.get("TOPOSPATIAL_CUBICASA_WEIGHTS")
    if env:
        candidates.append(Path(env).expanduser())
    prof = load_profile()
    if prof:
        candidates.append(Path(prof["weights"]))
    dirs = [Path(d).expanduser() for d in search_dirs]
    root = Path(__file__).resolve().parents[2] / "models"
    dirs += [root / "checkpoints", root / "pretrained", root, Path.home() / "Documents" / "TopoSpatial Exports" / "models"]
    for d in dirs:
        candidates += [d / name for name in WEIGHT_NAMES]
    return next((p for p in candidates if p.is_file()), None)


def status(weights: Optional[str] = None, search_dirs: Iterable[str] = ()) -> Tuple[bool, str]:
    """(usable?, reason) without loading the model."""
    try:
        import torch  # noqa: F401
    except ImportError:
        return False, "PyTorch is not installed in the server environment"
    path = find_weights(weights, search_dirs)
    if path is None:
        return False, "CubiCasa5K weights (model_best_val_loss_var.pkl) not found"
    return True, str(path)


def load_model(path: Path):
    key = str(path)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    import torch

    from ._cubicasa_model import hg_furukawa_original

    model = hg_furukawa_original(n_classes=51)
    n = sum(_SPLIT)
    model.conv4_ = torch.nn.Conv2d(256, n, bias=True, kernel_size=1)
    model.upsample = torch.nn.ConvTranspose2d(n, n, kernel_size=4, stride=4)
    try:
        checkpoint = torch.load(str(path), map_location="cpu", weights_only=True)
    except Exception:  # older pickles carry non-tensor training state
        logger.warning("weights_only load failed for %s; loading full pickle", path)
        checkpoint = torch.load(str(path), map_location="cpu", weights_only=False)
    state = checkpoint.get("model_state", checkpoint) if isinstance(checkpoint, dict) else checkpoint
    model.load_state_dict(state)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()
    _MODEL_CACHE[key] = model
    return model


def segment(
    image_bgr: np.ndarray,
    bbox: Optional[Tuple[int, int, int, int]],
    weights: Path,
    pad: int = 32,
    max_side: int = 1600,
    wall_logit_bias: float = 0.0,
    return_heat: bool = False,
) -> Dict[str, np.ndarray]:
    """Run the network on the plan region. Returns full-image uint8 masks
    ``walls``, ``windows``, ``doors`` and the room-class map ``rooms``.

    ``return_heat`` adds ``heat``: the 21 junction heatmaps as uint8
    (value x 255), shape (21, H, W), zero outside the network crop. Channels:
    0-3 wall ends, 4-7 corners, 8-11 T junctions, 12 cross, 13-16 opening
    ends (left, right, upper, lower), 17-20 icon corners.

    ``wall_logit_bias`` is added to the Wall logit before the arg-max: > 0
    keeps more wall pixels (higher recall), < 0 fewer. 0 is the plain arg-max."""
    import torch
    import torch.nn.functional as F

    h, w = image_bgr.shape[:2]
    if bbox is None:
        x0, y0, x1, y1 = 0, 0, w, h
    else:
        bx, by, bw, bh = bbox
        x0, y0 = max(0, bx - pad), max(0, by - pad)
        x1, y1 = min(w, bx + bw + pad), min(h, by + bh + pad)
    crop = image_bgr[y0:y1, x0:x1, ::-1].astype(np.float32)  # RGB
    ch, cw = crop.shape[:2]
    scale = min(1.0, max_side / float(max(ch, cw)))
    tensor = torch.from_numpy(np.ascontiguousarray(crop.transpose(2, 0, 1)))[None]
    tensor = 2.0 * (tensor / 255.0) - 1.0
    if scale < 1.0:
        tensor = F.interpolate(tensor, scale_factor=scale, mode="bilinear", align_corners=False)
    th, tw = tensor.shape[2:]
    ph, pw = (-th) % 64, (-tw) % 64
    tensor = F.pad(tensor, (0, pw, 0, ph), value=1.0)  # white

    model = load_model(weights)
    device = next(model.parameters()).device
    tensor = tensor.to(device)
    with torch.no_grad():
        out = model(tensor)[:, :, :th, :tw]
        if scale < 1.0:
            out = F.interpolate(out, size=(ch, cw), mode="bilinear", align_corners=False)
    heat, rooms, icons = torch.split(out[0].cpu(), list(_SPLIT), 0)
    if wall_logit_bias:
        rooms = rooms.clone()
        rooms[ROOM_CLASSES.index("Wall")] += float(wall_logit_bias)
    room_map = rooms.argmax(0).numpy().astype(np.uint8)
    icon_map = icons.argmax(0).numpy().astype(np.uint8)

    def full(mask: np.ndarray) -> np.ndarray:
        canvas = np.zeros((h, w), np.uint8)
        canvas[y0:y1, x0:x1] = mask
        return canvas

    result = {
        "walls": full((room_map == ROOM_CLASSES.index("Wall")).astype(np.uint8)),
        "windows": full((icon_map == ICON_CLASSES.index("Window")).astype(np.uint8)),
        "doors": full((icon_map == ICON_CLASSES.index("Door")).astype(np.uint8)),
        "rooms": full(room_map),
        "icons": full(icon_map),
    }
    if return_heat:
        canvas = np.zeros((heat.shape[0], h, w), np.uint8)
        canvas[:, y0:y1, x0:x1] = np.clip(np.rint(heat.float().numpy() * 255.0), 0, 255).astype(np.uint8)
        result["heat"] = canvas
    return result
