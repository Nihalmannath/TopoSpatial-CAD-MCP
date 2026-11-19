"""Neural wall-thickness guard (3 Nov 2026: fine-tuned mask on 6_SIXTH_15/1jca estimated 48 px)."""
import numpy as np

from trace_engine.vision import band_width_median, estimate_wall_thickness, mask_wall_thickness


def _walls(width: int = 20) -> np.ndarray:
    m = np.zeros((600, 600), np.uint8)
    for c in (100, 300, 500):
        m[c - width // 2:c + width // 2, 50:550] = 1   # horizontal walls
        m[50:550, c - width // 2:c + width // 2] = 1   # vertical walls
    return m


def test_clean_mask_keeps_the_run_length_estimate():
    m = _walls(20)
    t, source = mask_wall_thickness(m)
    assert source == "run-length peak"
    assert abs(t - estimate_wall_thickness(m, min_px=4)) < 1e-9
    assert 18 <= t <= 22


def test_blocky_ragged_mask_falls_back_to_the_band_median():
    m = _walls(20)
    for cell in (110, 310):                                  # 48 px blobs inside each room, apart
        for cx in (cell + 6, cell + 66, cell + 126):         # from walls and from each other
            for cy in (cell + 6, cell + 66, cell + 126):
                m[cy:cy + 48, cx:cx + 48] = 1
                m[cx:cx + 48, cy:cy + 48] = 1
    m[np.random.default_rng(0).random(m.shape) < 0.01] = 1   # speckle
    assert estimate_wall_thickness(m, min_px=4) > 1.3 * band_width_median(m)   # the failure mode
    t, source = mask_wall_thickness(m)
    assert source.startswith("band median") and 18 <= t <= 24


def test_empty_mask_has_a_safe_default():
    t, source = mask_wall_thickness(np.zeros((100, 100), np.uint8))
    assert t == 10.0 and source.startswith("default")
