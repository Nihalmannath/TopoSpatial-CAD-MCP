"""Local, revisioned pixel annotation storage and HTTP service."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .neural import ICON_CLASSES, ROOM_CLASSES

MAX_PIXELS = 16_000_000


def png_bytes(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def encode_mask(array):
    return base64.b64encode(png_bytes(array)).decode("ascii")


def decode_mask(encoded, size, classes):
    """Accept lossless indexed PNG or equal-channel browser canvas PNG."""
    if len(encoded) > 32_000_000:
        raise ValueError("Mask upload is too large")
    raw = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG" or image.size != size:
            raise ValueError("Mask must be a PNG with the original image dimensions")
        array = np.array(image)
    if array.ndim == 3:
        if array.shape[2] not in (3, 4) or not np.array_equal(array[:, :, 0], array[:, :, 1]) or not np.array_equal(array[:, :, 0], array[:, :, 2]):
            raise ValueError("Upload class IDs, not a colored overlay")
        if array.shape[2] == 4 and not (array[:, :, 3] == 255).all():
            raise ValueError("Mask alpha must be opaque")
        array = array[:, :, 0]
    if array.ndim != 2 or not np.isin(array, list(range(classes)) + [255]).all():
        raise ValueError("Mask contains invalid class IDs")
    return array.astype(np.uint8)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AnnotationStore:
    def __init__(self, source, output, survey=None, training_output=None, coco_output=None, pilot=None):
        self.source, self.output = Path(source).resolve(), Path(output).resolve()
        self.lock = threading.RLock()
        self.training_output = Path(training_output) if training_output else self.output / "training"
        self.coco_output = Path(coco_output) if coco_output else self.output / "coco"
        self.pilot = json.loads(Path(pilot).read_text(encoding="utf-8")) if pilot and Path(pilot).exists() else {"plans": []}
        self.assignments = {p["source_sha256"][:24]: p for p in self.pilot["plans"]}
        self.entries = {}
        styles = {}
        if survey and Path(survey).is_file():
            with Path(survey).open(encoding="utf-8-sig", newline="") as handle:
                styles = {row["filename"]: row.get("style", "unknown") for row in csv.DictReader(handle)}
        if not self.source.is_dir():
            raise ValueError(f"Image folder does not exist: {self.source}")
        for path in sorted(self.source.iterdir()):
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            key = digest[:24]
            if key not in self.entries:
                self.entries[key] = {"id": key, "name": path.name, "path": path,
                                     "sha256": digest, "style": styles.get(path.name, "unknown")}

    def entry(self, key):
        if key not in self.entries:
            raise KeyError("Unknown plan")
        return self.entries[key]

    def image(self, key):
        entry = self.entry(key)
        raw = entry["path"].read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("Source image changed; restart the labeler")
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("Image exceeds 16 million pixels; resize a copy before labeling")
            return ImageOps.exif_transpose(image).convert("RGB")

    def state_path(self, key):
        self.entry(key)
        return self.output / "drafts" / key / "current.json"

    def metadata(self, key):
        pointer = self.state_path(key)
        assignment = self.assignments.get(key, {})
        return json.loads(pointer.read_text(encoding="utf-8")) if pointer.exists() else {
            "revision": 0, "annotation_complete": False, "split": "train", "group_id": key,
            "polygons_complete": False, "polygons": [], "group_verified": False,
            **{k: assignment[k] for k in ("split", "group_id") if k in assignment},
        }

    def listing(self):
        return [{"id": key, "name": item["name"], "style": item["style"], "pilot": key in self.assignments, **self.metadata(key)}
                for key, item in self.entries.items()]

    def load(self, key):
        with self.lock:
            image = self.image(key)
            meta = self.metadata(key)
            masks = {}
            for task in ("rooms", "icons"):
                if meta["revision"]:
                    path = self.state_path(key).parent / str(meta["revision"]) / f"{task}.png"
                    masks[task] = base64.b64encode(path.read_bytes()).decode("ascii")
                else:
                    masks[task] = encode_mask(np.full((image.height, image.width), 255, np.uint8))
            return {**meta, **masks, "width": image.width, "height": image.height}

    def save(self, key, payload):
        with self.lock:
            image = self.image(key)
            previous = self.metadata(key)
            if payload.revision != previous["revision"]:
                raise RuntimeError("This plan changed in another tab. Reload before saving.")
            rooms = decode_mask(payload.rooms, image.size, len(ROOM_CLASSES))
            icons = decode_mask(payload.icons, image.size, len(ICON_CLASSES))
            from .vector_annotations import validate_polygons
            polygons = validate_polygons(payload.polygons, image.size)
            if payload.polygons_reviewed and (not polygons or not payload.group_verified):
                raise ValueError("Review polygon completeness and building/group assignment before export")
            if payload.reviewed and not ((rooms != 255).any() or (icons != 255).any()):
                raise ValueError("Nothing is annotated. Label pixels before exporting.")
            group = payload.group_id.strip() or key
            for other in self.entries:
                meta = self.metadata(other)
                if other != key and meta.get("group_id") == group and meta.get("split") != payload.split:
                    raise ValueError("Plans from the same building/group must use the same split")
            revision = previous["revision"] + 1
            folder = self.state_path(key).parent / str(revision)
            meta = {"revision": revision, "annotation_complete": payload.reviewed,
                    "split": payload.split, "group_id": group,
                    "source_image": str(self.entry(key)["path"]),
                    "source_sha256": self.entry(key)["sha256"],
                    "image_size": list(image.size), "orientation": "EXIF-normalized",
                    "room_classes": ROOM_CLASSES, "icon_classes": ICON_CLASSES,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "provenance": payload.provenance,
                    "polygons": polygons, "polygons_complete": payload.polygons_reviewed,
                    "group_verified": payload.group_verified,
                    "labeled_pixels": {"rooms": int((rooms != 255).sum()), "icons": int((icons != 255).sum())}}
            for task, mask in (("rooms", rooms), ("icons", icons)):
                atomic_write(folder / f"{task}.png", png_bytes(mask))
            atomic_write(folder / "meta.json", json.dumps(meta, indent=2).encode())
            # Invalidate an older export before changing its files. A failed write
            # cannot leave an incomplete example marked ready for training.
            export = self.training_output / key
            if export.exists() or payload.reviewed:
                atomic_write(export / "meta.json", json.dumps({**meta, "annotation_complete": False}).encode())
            if payload.reviewed:
                atomic_write(export / "image.png", png_bytes(np.asarray(image)))
                atomic_write(export / "rooms.png", png_bytes(rooms))
                atomic_write(export / "icons.png", png_bytes(icons))
                atomic_write(export / "meta.json", json.dumps(meta, indent=2).encode())
            atomic_write(self.state_path(key), json.dumps(meta, indent=2).encode())
            # Rebuild the reviewed-only snapshot, removing any plan invalidated
            # by saving edits as a draft. Revisions remain the source of truth.
            from .vector_annotations import export_coco
            export_coco(self, self.coco_output)
            return meta


def create_app(source, output, survey=None, weights=None, training_output=None, coco_output=None, pilot=None):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, Response
    from pydantic import BaseModel, Field
    from typing import Literal

    # Keep model annotations resolvable with postponed module annotations.
    class SaveRequest(BaseModel):
        revision: int = Field(ge=0)
        rooms: str
        icons: str
        reviewed: bool = False
        split: Literal["train", "val"] = "train"
        group_id: str = Field(default="", max_length=200)
        provenance: str = Field(default="manual", max_length=500)
        polygons: list[dict] = Field(default_factory=list, max_length=200)
        polygons_reviewed: bool = False
        group_verified: bool = False

    store = AnnotationStore(source, output, survey, training_output, coco_output, pilot)
    app = FastAPI(title="Plan Labeler", docs_url=None, redoc_url=None)
    app.state.store = store
    token = secrets.token_urlsafe(32)
    app.state.token = token
    inference_lock = threading.Lock()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return Response("Local requests only", status_code=403)
        if request.method == "POST" and request.headers.get("x-labeler-token") != token:
            return Response("Invalid session token", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Unknown plan"}, status_code=404)

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = Path(__file__).with_name("labeler_web").joinpath("index.html").read_text(encoding="utf-8")
        return page.replace("__SESSION_TOKEN__", token)

    @app.get("/api/plans")
    def plans():
        from .vector_annotations import CATEGORIES
        return {"plans": store.listing(), "room_classes": ROOM_CLASSES, "icon_classes": ICON_CLASSES,
                "vector_classes": CATEGORIES, "coco_path": str(store.coco_output),
                "training_path": str(store.training_output)}

    @app.get("/api/plans/{key}/image")
    def image(key: str):
        return Response(png_bytes(np.asarray(store.image(key))), media_type="image/png")

    @app.get("/api/plans/{key}")
    def load(key: str):
        return store.load(key)

    # Assign concrete annotation because SaveRequest is scoped to the factory.
    def save(key, payload):
        try:
            return store.save(key, payload)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
    save.__annotations__ = {"key": str, "payload": SaveRequest}
    app.post("/api/plans/{key}")(save)

    @app.post("/api/plans/{key}/predict")
    def predict(key: str):
        from .neural import find_weights, segment
        path = find_weights(weights)
        if path is None:
            raise HTTPException(503, "Pretrained weights were not found. Manual labeling remains available.")
        if not inference_lock.acquire(blocking=False):
            raise HTTPException(409, "A prediction is already running. Try again when it finishes.")
        try:
            image = np.asarray(store.image(key))
            result = segment(image[:, :, ::-1].copy(), None, path, max_side=1280)
            return {"rooms": encode_mask(result["rooms"]), "icons": encode_mask(result["icons"]),
                    "provenance": f"model draft: {path.name}; manually review before export"}
        except (ImportError, RuntimeError) as exc:
            raise HTTPException(503, f"Prediction failed: {exc}") from exc
        finally:
            inference_lock.release()

    return app
