import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from stage_trace import lost_blobs, runs_mask  # noqa: E402
from trace_engine.vision import Run  # noqa: E402


def test_runs_mask_covers_the_band_and_lost_blobs_names_the_rule():
    shape = (200, 200)
    covered = runs_mask([Run("h", 50.0, 10.0, 190.0, 18.0)], shape, grow=3)
    assert covered[50, 100] and not covered[120, 100]
    mask = np.zeros(shape, np.uint8)
    mask[40:60, 10:190] = 1          # the covered wall
    mask[100:160, 100:106] = 1       # a 6 px wide, 60 px long wall nobody extracted
    blobs = lost_blobs(mask, covered, thickness=18.0, min_area=100)
    assert len(blobs) == 1 and blobs[0]["h"] == 60 and "thinner" in blobs[0]["why"]
