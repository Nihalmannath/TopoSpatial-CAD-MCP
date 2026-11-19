"""Training label contract and loss regression checks."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("finetune", ROOT / "scripts" / "finetune_cubicasa.py")
training = importlib.util.module_from_spec(spec)
spec.loader.exec_module(training)


def example(root, rooms=None):
    folder = root / "sample"
    folder.mkdir()
    Image.fromarray(np.full((8, 8, 3), 255, np.uint8)).save(folder / "image.png")
    Image.fromarray(np.zeros((8, 8), np.uint8) if rooms is None else rooms).save(folder / "rooms.png")
    Image.fromarray(np.zeros((8, 8), np.uint8)).save(folder / "icons.png")
    return folder


@pytest.mark.parametrize("mask", [np.zeros((7, 8), np.uint8), np.full((8, 8), 12, np.uint8)])
def test_reject_invalid_labels(tmp_path, mask):
    example(tmp_path, mask)
    with pytest.raises(ValueError):
        training.load_examples(tmp_path)


def test_ignore_mask_and_padding(tmp_path):
    folder = example(tmp_path)
    Image.fromarray(np.full((8, 8), 255, np.uint8)).save(folder / "ignore.png")
    _, image, rooms, icons, wmap, pts = training.load_examples(tmp_path)[0]
    assert pts is None  # no graph.json: no junction labels
    _, rooms, icons, _, pts = training.augment(image, rooms, icons, wmap, pts, 128)
    assert (rooms == 255).all() and (icons == 255).all() and pts is None


def test_all_ignored_loss_has_zero_gradient():
    logits = torch.randn(1, 12, 4, 4, requires_grad=True)
    loss = training.masked_cross_entropy(logits, torch.full((1, 4, 4), 255), torch.ones(12))
    loss.backward()
    assert loss.item() == 0
    assert torch.count_nonzero(logits.grad) == 0


def test_ignored_pixels_do_not_change_loss():
    logits = torch.randn(1, 12, 2, 2)
    labels = torch.tensor([[[2, 255], [255, 255]]])
    expected = torch.nn.functional.cross_entropy(logits[:, :, :1, :1], labels[:, :1, :1])
    assert torch.allclose(training.masked_cross_entropy(logits, labels, torch.ones(12)), expected)
