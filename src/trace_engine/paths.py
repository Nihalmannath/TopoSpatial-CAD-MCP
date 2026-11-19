"""Where tracing data lives. One place, overridable, no hard-coded user paths.

``TOPOSPATIAL_DATA`` overrides the data root; the default is ``<repo>/dataset``.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_root() -> Path:
    env = os.environ.get("TOPOSPATIAL_DATA")
    return Path(env).expanduser() if env else REPO_ROOT / "dataset"


def vector_exports_root() -> Path:
    """Per-project folders of plan images + IFC/PDF exports."""
    return data_root() / "raw_vector_exports" / "PDF plan"


def gt_root() -> Path:
    """Output of scripts/build_gt_dataset.py (candidates/, training/, review.html)."""
    return data_root() / "gt"
