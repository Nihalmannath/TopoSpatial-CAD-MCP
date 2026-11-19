"""Neural engine plumbing: CubiCasa-style masks flow through the tracer.

The network itself needs PyTorch + weights, so ``segment`` is replaced with
masks derived from a rendered plan, in both styles the network can produce:
walls with gaps at openings, and continuous walls with icon masks on top."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_engine import compile_trace, neural, vision  # noqa: E402
from trace_engine._cv import cv2  # noqa: E402
from trace_fixtures import render_reference_plan  # noqa: E402


@pytest.fixture()
def plan(tmp_path):
    img, _ = render_reference_plan()
    path = tmp_path / "plan.png"
    img.save(path)
    return str(path)


def _masks(path):
    gray = cv2.cvtColor(cv2.imread(path, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
    dark = vision.drop_frame(vision._dark_mask(gray))
    t = vision.estimate_wall_thickness(dark)
    walls = vision.wall_mask(gray, t) & dark
    _, report = vision.trace_image(path, ocr=False, mm_per_px=10.0)
    win, door = np.zeros_like(walls), np.zeros_like(walls)
    for o in report.openings:
        m = win if o.kind == "window" else door
        a, b, c0, c1 = int(o.lo), int(o.hi), int(o.c - t / 2), int(o.c + t / 2)
        if o.axis == "h":
            m[c0:c1, a:b] = 1
        else:
            m[a:b, c0:c1] = 1
    return walls, win, door


@pytest.mark.parametrize("continuous", [False, True])
def test_neural_masks_trace_like_classic(plan, monkeypatch, continuous):
    walls, win, door = _masks(plan)
    seg_walls = (walls | win | door) if continuous else walls
    monkeypatch.setattr(neural, "find_weights", lambda *a, **k: Path("fake.pkl"))
    monkeypatch.setattr(neural, "segment", lambda image, region, path, **k: {
        "walls": seg_walls, "windows": win, "doors": door, "rooms": seg_walls})
    classic = compile_trace(vision.trace_image(plan, ocr=False, mm_per_px=10.0)[0]).summary()["counts"]
    trace, report = vision.trace_image(plan, ocr=False, mm_per_px=10.0, engine="neural")
    counts = compile_trace(trace).summary()["counts"]
    assert report.engine == "neural"
    # Continuous-wall masks rely on icon blobs; allow one miss per kind.
    assert abs(counts.get("door", 0) - classic.get("door", 0)) <= (1 if continuous else 0)
    assert abs(counts.get("window", 0) - classic.get("window", 0)) <= (1 if continuous else 0)
    assert abs(counts.get("room", 0) - classic.get("room", 0)) <= 1


def test_neural_status_reports_missing_pieces(monkeypatch, tmp_path):
    ok, reason = neural.status(str(tmp_path / "nope.pkl"), [str(tmp_path)])
    assert not ok and ("PyTorch" in reason or "weights" in reason)


def _profile(tmp_path, monkeypatch, weights, bias=2.0, **extra):
    import json

    prof = tmp_path / "neural_profile.json"
    prof.write_text(json.dumps({"weights": str(weights), "wall_logit_bias": bias, **extra}))
    monkeypatch.setattr(neural, "PROFILE_PATH", prof)
    return prof


def test_promoted_profile_picks_weights_and_bias(tmp_path, monkeypatch):
    promoted = tmp_path / "pretrained.pkl"
    other = tmp_path / "cubicasa_finetuned.pth"
    promoted.write_bytes(b"x")
    other.write_bytes(b"y")
    monkeypatch.delenv("TOPOSPATIAL_CUBICASA_WEIGHTS", raising=False)
    _profile(tmp_path, monkeypatch, promoted, 2.0, geometry_pipeline="canonical")
    # The promoted weights win over a fine-tuned checkpoint in the search dirs.
    assert neural.find_weights(None, [str(tmp_path)]) == promoted
    assert neural.default_wall_bias(promoted) == 2.0
    assert neural.default_wall_bias(other) == 0.0  # bias belongs to the promoted weights only
    assert neural.find_weights(str(other)) == other  # explicit weights still win
    assert neural.load_profile()["geometry_pipeline"] == "canonical"


def test_missing_or_broken_profile_is_ignored(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, tmp_path / "gone.pkl")
    assert neural.load_profile() == {}
    (tmp_path / "neural_profile.json").write_text("{not json")
    assert neural.load_profile() == {} and neural.default_wall_bias(tmp_path / "x.pkl") == 0.0


def test_trace_image_applies_the_promoted_bias(plan, tmp_path, monkeypatch):
    walls, win, door = _masks(plan)
    weights = tmp_path / "pretrained.pkl"
    weights.write_bytes(b"x")
    _profile(tmp_path, monkeypatch, weights, 2.0)
    seen = []

    def fake_segment(image, region, path, wall_logit_bias=0.0, **k):
        seen.append(wall_logit_bias)
        return {"walls": walls, "windows": win, "doors": door, "rooms": walls}

    monkeypatch.setattr(neural, "segment", fake_segment)
    _, report = vision.trace_image(plan, ocr=False, mm_per_px=10.0, engine="neural", weights=str(weights))
    _, _ = vision.trace_image(plan, ocr=False, mm_per_px=10.0, engine="neural", weights=str(weights),
                              wall_logit_bias=0.5)
    assert seen == [2.0, 0.5] and report.wall_logit_bias == 2.0


def test_given_room_sizes_set_the_scale_when_ocr_labels_have_none(plan, monkeypatch):
    # OCR finds text but no usable sizes (as on hatched brochure plans): the
    # room sizes the user gave must still calibrate the scale.
    from tests.test_trace_engine import ROOMS

    # Sizes were read, but the labels sit outside every traced room (as on the
    # hatched plan), so they cannot calibrate - and they skip the old fallback.
    garbled = [vision.Label("Toilet Va", (5, 5, 40, 12), (168.0, 132.0)),
               vision.Label("Pray!", (9, 40, 30, 12), (400.0, 510.0))]
    monkeypatch.setattr(vision, "read_labels", lambda *a, **k: list(garbled))
    sizes = [(n, (xr - xl) / 10, (yt - yb) / 10) for n, (xl, xr, yb, yt) in ROOMS.items()]
    trace, report = vision.trace_image(plan, ocr=True, room_sizes=sizes, nominal_wall_mm=400)
    assert not report.scale_method.startswith("nominal"), report.scale_method
    assert report.mm_per_px == pytest.approx(10.0, rel=0.03)  # the fixture is drawn at 10 mm/px
    names = {z.name for z in trace.zones_list}
    assert len(names & set(ROOMS)) >= 6


def test_pad_region_contains_outer_walls_and_clamps():
    from trace_engine.vision import pad_region

    assert pad_region((33, 33, 967, 1045), 24, 1070, 1250) == (9, 9, 1015, 1093)
    assert pad_region((5, 5, 100, 100), 24, 110, 110) == (0, 0, 110, 110)
