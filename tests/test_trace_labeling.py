"""Mask editor storage, export contract, and HTTP boundary tests."""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from trace_engine.labeling import create_app, decode_mask, encode_mask


@pytest.fixture
def client(tmp_path):
    source = tmp_path / "images"
    source.mkdir()
    Image.fromarray(np.full((16, 24, 3), 230, np.uint8)).save(source / "one.png")
    Image.fromarray(np.full((16, 24, 3), 210, np.uint8)).save(source / "two.png")
    app = create_app(source, tmp_path / "annotations")
    with TestClient(app) as client:
        yield client


def headers(client):
    return {"X-Labeler-Token": client.app.state.token}


def payload(revision=0, reviewed=True):
    room = np.full((16, 24), 255, np.uint8)
    room[2:4, :] = 2
    return {"revision": revision, "rooms": encode_mask(room),
            "icons": encode_mask(np.full((16, 24), 255, np.uint8)),
            "reviewed": reviewed, "split": "train", "group_id": "building-a"}


def test_new_masks_are_ignored_and_ui_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "Plan Labeler" in page.text
    assert "__SESSION_TOKEN__" not in page.text
    key = client.get("/api/plans").json()["plans"][0]["id"]
    state = client.get(f"/api/plans/{key}").json()
    assert (decode_mask(state["rooms"], (24, 16), 12) == 255).all()
    assert state["revision"] == 0


def test_reviewed_export_roundtrip_and_stale_save(client):
    key = client.get("/api/plans").json()["plans"][0]["id"]
    url = f"/api/plans/{key}"
    response = client.post(url, json=payload(), headers=headers(client))
    assert response.status_code == 200, response.text
    export = client.app.state.store.output / "training" / key
    assert json.loads((export / "meta.json").read_text())["annotation_complete"]
    mask = np.array(Image.open(export / "rooms.png"))
    assert mask.shape == (16, 24) and mask[2, 0] == 2 and mask[0, 0] == 255
    assert Image.open(export / "image.png").size == (24, 16)
    assert client.post(url, json=payload(), headers=headers(client)).status_code == 409
    assert client.post(url, json=payload(1, False), headers=headers(client)).status_code == 200
    assert not json.loads((export / "meta.json").read_text())["annotation_complete"]
    assert (client.app.state.store.output / "drafts" / key / "1" / "rooms.png").exists()


def test_draft_does_not_export_and_group_cannot_leak(client):
    keys = [p["id"] for p in client.get("/api/plans").json()["plans"]]
    assert client.post(f"/api/plans/{keys[0]}", json=payload(reviewed=False), headers=headers(client)).status_code == 200
    assert not (client.app.state.store.output / "training" / keys[0]).exists()
    other = payload()
    other["split"] = "val"
    assert client.post(f"/api/plans/{keys[1]}", json=other, headers=headers(client)).status_code == 400


def test_bad_masks_and_unprotected_posts_rejected(client):
    key = client.get("/api/plans").json()["plans"][0]["id"]
    url = f"/api/plans/{key}"
    assert client.post(url, json=payload()).status_code == 403
    for mask in (np.full((16, 24), 12, np.uint8), np.zeros((4, 4), np.uint8)):
        bad = payload()
        bad["rooms"] = encode_mask(mask)
        assert client.post(url, json=bad, headers=headers(client)).status_code == 400
    assert client.get("/api/plans/not-an-id").status_code == 404


def test_export_loads_with_trainer_and_explicit_split(client):
    pytest.importorskip("torch")
    spec = importlib.util.spec_from_file_location("training_for_label_test", ROOT / "scripts" / "finetune_cubicasa.py")
    trainer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trainer)
    keys = [p["id"] for p in client.get("/api/plans").json()["plans"]]
    for n, key in enumerate(keys):
        data = payload()
        data.update(split="val" if n else "train", group_id=f"building-{n}")
        assert client.post(f"/api/plans/{key}", json=data, headers=headers(client)).status_code == 200
    root = client.app.state.store.output / "training"
    examples = trainer.load_examples(root, require_reviewed=True)
    train, val = trainer.partition_examples(examples, root, 0)
    assert [ex[0] for ex in train] == keys[:1]
    assert [ex[0] for ex in val] == keys[1:]
    meta_path = root / keys[1] / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["group_id"] = "building-0"
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="both train and validation"):
        trainer.partition_examples(examples, root, 0)


def test_predictions_are_unsaved_drafts(client, monkeypatch, tmp_path):
    from trace_engine import neural
    monkeypatch.setattr(neural, "find_weights", lambda _: tmp_path / "fake.pkl")
    monkeypatch.setattr(neural, "segment", lambda *a, **kw: {
        "rooms": np.full((16, 24), 2, np.uint8), "icons": np.zeros((16, 24), np.uint8)})
    key = client.get("/api/plans").json()["plans"][0]["id"]
    result = client.post(f"/api/plans/{key}/predict", json={}, headers=headers(client))
    assert result.status_code == 200
    assert (decode_mask(result.json()["rooms"], (24, 16), 12) == 2).all()
    assert client.app.state.store.metadata(key)["revision"] == 0


def test_reviewed_vector_coco_and_invalid_polygon(client):
    key = client.get("/api/plans").json()["plans"][0]["id"]
    data = payload(reviewed=False)
    data.update(polygons=[{"category_id": 3, "points": [[2,2],[12,2],[12,10],[2,10]]}],
                polygons_reviewed=True, group_verified=True)
    assert client.post(f"/api/plans/{key}", json=data, headers=headers(client)).status_code == 200
    document = json.loads((client.app.state.store.coco_output / "annotations/train.json").read_text())
    assert document["categories"][3]["name"] == "Bed Room"
    assert document["annotations"][0]["area"] == 80
    assert document["annotations"][0]["bbox"] == [2,2,10,8]
    data["revision"] = 1
    data["polygons"][0]["points"] = [[2,2],[12,10],[12,2],[2,10]]
    assert client.post(f"/api/plans/{key}", json=data, headers=headers(client)).status_code == 400
    assert client.app.state.store.metadata(key)["revision"] == 1
