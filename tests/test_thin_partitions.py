from trace_engine.vision import Run, drop_ladders


def test_stair_treads_are_dropped_but_a_lone_partition_stays():
    treads = [Run("v", 150.0 + 20 * i, 330.0, 570.0, 2.0) for i in range(4)]
    partition = Run("v", 700.0, 100.0, 300.0, 2.0)
    kept = drop_ladders(treads + [partition], max_gap=57.0)
    assert kept == [partition]


def test_a_pair_of_parallel_lines_is_kept():
    pair = [Run("h", 200.0, 0.0, 100.0, 2.0), Run("h", 230.0, 0.0, 100.0, 2.0)]
    assert drop_ladders(pair, max_gap=57.0) == pair


def test_close_lines_that_do_not_overlap_are_not_a_ladder():
    spread = [Run("h", 200.0 + 20 * i, 100.0 * i, 100.0 * i + 60.0, 2.0) for i in range(4)]
    assert drop_ladders(spread, max_gap=57.0) == spread


def test_widely_spaced_parallel_lines_are_kept():
    rooms = [Run("v", 100.0 + 200 * i, 0.0, 300.0, 2.0) for i in range(4)]
    assert drop_ladders(rooms, max_gap=57.0) == rooms


# --- band extraction (3 Nov 2026: toilet/store partition on ground_floor_plan.jpg) -------------
import numpy as np  # noqa: E402

from trace_engine.vision import _same_band, extract_runs, keep_band  # noqa: E402


def _tapered_partition() -> np.ndarray:
    """The measured failure: columns 469-479 run y 75..214, column 480 stops at 192 where the mask
    tapers into a junction, column 468 only covers 75..112. Dominant wall thickness 22.1 px."""
    mask = np.zeros((300, 600), np.uint8)
    mask[75:214, 469:480] = 1
    mask[75:192, 480] = 1
    mask[75:112, 468] = 1
    return mask


def test_tapered_thin_partition_is_extracted_as_one_wall():
    runs = [r for r in extract_runs(_tapered_partition(), 22.1) if r.axis == "v"]
    assert len(runs) == 1
    r = runs[0]
    assert abs(r.c - 474.5) <= 1.0 and r.t >= 12 and r.lo <= 76 and r.hi >= 212


def test_same_band_accepts_a_short_edge_row_but_not_a_different_wall():
    assert _same_band(75, 192, 75, 214, tol=11.05)        # ends 22 px early, 84 % overlap
    assert not _same_band(75, 112, 75, 214, tol=11.05)    # a stub, 27 % overlap
    assert not _same_band(300, 420, 75, 214, tol=11.05)   # another wall further along


def test_keep_band_needs_length_for_thin_bands():
    t = 18.9
    assert keep_band(16, 30, t)          # ordinary wall width: kept at any length
    assert keep_band(9, 140, t)          # thin but long and straight: a partition
    assert not keep_band(9, 30, t)       # thin and short: cushion / fixture outline
    assert not keep_band(3, 300, 6.6)    # under the absolute floor: linework
