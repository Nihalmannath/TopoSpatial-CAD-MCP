"""Corrected drawings rasterise into aligned CubiCasa-style labels."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from trace_engine.dataset import Alignment, DrawingContent, rasterize, room_class, write_example  # noqa: E402
from trace_engine.neural import ICON_CLASSES, ROOM_CLASSES  # noqa: E402


def _content():
    walls = [((0, 0), (4000, 0), 200), ((4000, 0), (4000, 3000), 200),
             ((4000, 3000), (0, 3000), 200), ((0, 3000), (0, 0), 200)]
    return DrawingContent(
        walls=walls,
        openings=[("door", (1000, -100), (1900, 800)), ("window", (2500, 2900), (3700, 3100))],
        rooms=[[(100, 100), (3900, 100), (3900, 2900), (100, 2900)]],
        texts=[("KITCHEN", (1500, 1500)), ("320X307", (1500, 1200))],
    )


def test_room_names_map_to_classes():
    cls = lambda n: ROOM_CLASSES[room_class(n)]  # noqa: E731
    assert cls("TOILET 1") == "Bath" and cls("BED ROOM 2") == "Bed Room"
    assert cls("SIT OUT") == "Outdoor" and cls("Drawing") == "Living Room"
    assert cls("Mystery") == "Undefined"


def test_labels_land_on_the_right_pixels():
    align = Alignment(10.0, (-500, 3500), (500, 400))
    rooms, icons, counts = rasterize(_content(), align)
    assert counts == {"walls": 4, "rooms": 1, "named_rooms": 1, "windows": 1, "doors": 1}
    assert rooms[350, 250] == ROOM_CLASSES.index("Wall")  # bottom wall at y=0 mm
    assert rooms[200, 250] == ROOM_CLASSES.index("Kitchen")
    ys, xs = np.where(icons == ICON_CLASSES.index("Door"))
    assert 330 <= ys.min() and ys.max() <= 362  # clipped to the wall, not the swing
    assert abs(xs.min() - 150) <= 2 and abs(xs.max() - 240) <= 2
    assert (icons == ICON_CLASSES.index("Window")).sum() > 0


def test_write_example(tmp_path):
    from PIL import Image

    img = tmp_path / "plan.png"
    Image.new("RGB", (500, 400), "white").save(img)
    info = write_example(_content(), Alignment(10.0, (-500, 3500), (500, 400)), str(img), tmp_path / "ds")
    folder = Path(info["folder"])
    assert {p.name for p in folder.iterdir()} >= {"image.png", "rooms.png", "icons.png", "preview.png", "meta.json"}
