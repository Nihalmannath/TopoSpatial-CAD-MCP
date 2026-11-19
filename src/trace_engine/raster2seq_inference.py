"""Production Raster2Seq inference with crop restoration and checkpoint gating.

The neural runtime intentionally remains isolated in ``.venv-raster2seq``.
This module only prepares an input, invokes the upstream predictor, converts
its JSON polygons to :class:`TraceResult`, and rejects topologically collapsed
checkpoints before any CAD changes are compiled.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

from .models import TraceResult
from .raster2seq_bridge import assess_trace_quality, raster2seq_to_trace

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PYTHON = PROJECT_ROOT / ".venv-raster2seq" / "Scripts" / "python.exe"
DEFAULT_RUNTIME = PROJECT_ROOT / "training" / "raster2seq_runtime"
DEFAULT_PRETRAINED = PROJECT_ROOT / "models" / "pretrained" / "raster2seq" / "checkpoint.pth"
DEFAULT_BEST = PROJECT_ROOT / "models" / "checkpoints" / "r2seq_pilot30_finetune" / "checkpoint_best.pth"
DEFAULT_MANIFEST = PROJECT_ROOT / "dataset" / "crop_manifest.json"


@dataclass
class Raster2SeqReport:
    mm_per_px: float
    wall_thickness_px: float
    scale_method: str
    checkpoint: str
    crop_box: Optional[Tuple[int, int, int, int]] = None
    candidate_quality: Dict[str, Dict[str, object]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def status(weights_path: Optional[str] = None) -> Tuple[bool, str]:
    """Return availability and a useful diagnostic for the isolated runtime."""
    required = [DEFAULT_PYTHON, DEFAULT_RUNTIME / "predict.py"]
    if weights_path:
        required.append(Path(weights_path).expanduser())
    else:
        required.append(DEFAULT_PRETRAINED)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return False, "missing Raster2Seq runtime file(s): " + ", ".join(missing)
    return True, str(Path(weights_path).expanduser() if weights_path else DEFAULT_PRETRAINED)


def _checkpoint_candidates(weights_path: Optional[str]) -> List[Path]:
    if weights_path:
        return [Path(weights_path).expanduser().resolve()]
    # The final pilot checkpoint is intentionally excluded: the held-out
    # evaluation proved it collapsed. The original model remains the safety
    # baseline while the best pilot checkpoint competes on topology quality.
    return [path for path in (DEFAULT_PRETRAINED, DEFAULT_BEST) if path.is_file()]


def _known_crop(image_path: Path, image_size: Tuple[int, int]) -> Optional[Tuple[int, int, int, int]]:
    if not DEFAULT_MANIFEST.is_file():
        return None
    try:
        manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        record = next((item for item in manifest.get("plans", []) if item.get("filename") == image_path.name), None)
        if not record:
            return None
        original = tuple(record.get("orig_size", []))
        cropped = tuple(record.get("cropped_size", []))
        # Only crop a full brochure. If the caller already supplied the
        # cropped file, its coordinate origin must stay local to that image.
        if image_size == original and image_size != cropped:
            box = tuple(int(value) for value in record["crop_box"])
            return box if len(box) == 4 else None
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        logger.warning("could not use crop manifest: %s", exc)
    return None


def _predict(input_dir: Path, output_dir: Path, checkpoint: Path, timeout_s: int) -> List[dict]:
    command = [
        str(DEFAULT_PYTHON), "-B", "-u", str(DEFAULT_RUNTIME / "predict.py"),
        "--dataset_name", "cubicasa",
        "--dataset_root", str(input_dir),
        "--checkpoint", str(checkpoint),
        "--output_dir", str(output_dir),
        "--semantic_classes", "12",
        "--input_channels", "3",
        "--poly2seq",
        "--seq_len", "512",
        "--num_bins", "32",
        "--disable_poly_refine",
        "--dec_attn_concat_src",
        "--per_token_sem_loss",
        "--use_anchor",
        "--ema4eval",
        "--save_pred",
        "--batch_size", "1",
        "--num_workers", "0",
    ]
    completed = subprocess.run(
        command,
        cwd=str(DEFAULT_RUNTIME),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (output_dir / "inference.log").write_text(
        (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
    )
    if completed.returncode:
        tail = " | ".join(((completed.stderr or "") + (completed.stdout or "")).strip().splitlines()[-6:])
        raise RuntimeError(
            f"Raster2Seq inference failed for {checkpoint.name} (exit {completed.returncode}): "
            f"{tail[-900:]}"
        )
    jsons = list(output_dir.glob("**/input.json"))
    if not jsons:
        raise RuntimeError(f"Raster2Seq produced no prediction JSON under {output_dir}")
    data = json.loads(jsons[0].read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Raster2Seq prediction JSON is not a list")
    return data


def trace_image(
    image_path: str | Path,
    *,
    mm_per_px: float = 10.0,
    nominal_wall_mm: float = 230.0,
    weights_path: Optional[str] = None,
    timeout_s: int = 600,
) -> Tuple[TraceResult, Raster2SeqReport]:
    """Run safe Raster2Seq inference and return the best non-collapsed trace."""
    image_path = Path(image_path).expanduser().resolve()
    available, detail = status(weights_path)
    if not available:
        raise RuntimeError(detail)
    if not (mm_per_px > 0):
        raise ValueError("mm_per_px must be positive")

    with Image.open(image_path) as source:
        source_size = source.size
        crop_box = _known_crop(image_path, source_size)
        inference_image = source.convert("RGB")
        origin = (0.0, 0.0)
        if crop_box is not None:
            inference_image = inference_image.crop(crop_box)
            origin = (float(crop_box[0]), float(crop_box[1]))
        inference_size = inference_image.size

        with tempfile.TemporaryDirectory(prefix="multicad-r2seq-") as temporary:
            temporary_path = Path(temporary)
            input_dir = temporary_path / "input"
            input_dir.mkdir()
            inference_image.save(input_dir / "input.png")

            traces = []
            quality_by_checkpoint: Dict[str, Dict[str, object]] = {}
            errors = []
            for index, checkpoint in enumerate(_checkpoint_candidates(weights_path), start=1):
                output_dir = temporary_path / f"output-{index}"
                output_dir.mkdir()
                try:
                    predictions = _predict(input_dir, output_dir, checkpoint, timeout_s)
                    trace = raster2seq_to_trace(
                        predictions,
                        inference_size,
                        mm_per_px=mm_per_px,
                        wall_thickness_mm=nominal_wall_mm,
                        origin_px=origin,
                    )
                    quality = assess_trace_quality(trace)
                    key = str(checkpoint)
                    quality_by_checkpoint[key] = quality.as_dict()
                    traces.append((quality.score, checkpoint, trace, quality))
                except Exception as exc:  # one candidate must not hide a viable fallback
                    logger.exception("Raster2Seq candidate failed: %s", checkpoint)
                    errors.append(f"{checkpoint.name}: {exc}")

    viable = [item for item in traces if not item[3].collapsed]
    if not viable:
        details = errors or [f"{item[1].name}: collapsed topology" for item in traces]
        raise RuntimeError("all Raster2Seq candidates were rejected: " + "; ".join(details))
    _, checkpoint, trace, _ = max(viable, key=lambda item: item[0])
    warnings = list(errors)
    if crop_box is not None:
        warnings.append(f"used audited plan crop {list(crop_box)} and restored original-image coordinates")
    warnings.append(f"selected {checkpoint.name} by topology quality; checkpoint_latest.pth is quarantined")
    return trace, Raster2SeqReport(
        mm_per_px=mm_per_px,
        wall_thickness_px=nominal_wall_mm / mm_per_px,
        scale_method="explicit mm_per_px" if mm_per_px != 10.0 else "default 10.0 mm/px",
        checkpoint=str(checkpoint),
        crop_box=crop_box,
        candidate_quality=quality_by_checkpoint,
        warnings=warnings,
    )

