# Plan Labeler

Run from the multiCAD-mcp repository, then open the printed address:

```powershell
.venv\Scripts\python.exe scripts\label_plans.py
```

Default address: http://127.0.0.1:8765. Default images: `models/Dataset`.
Default working data: `models/annotations`. Nothing is uploaded or sent to CAD.
The app uses existing FastAPI, Uvicorn, Pillow and NumPy dependencies.
PyTorch and the pretrained weights are only needed for model drafts.

Override folders with `--images PATH --output PATH`; use `--port 8766` if needed.
Raster JPG, PNG, WebP, TIFF and BMP are supported. Convert PDFs to raster first.
EXIF orientation is normalized consistently in the displayed image and export.
Images over 16 million pixels are rejected; resize a copy before annotating.

## Label and review

1. Filter by drawing style, then choose a plan. New masks start as 255 (ignore).
2. Optionally generate a model draft. It replaces both in-memory layers after
   confirmation. Save important edits first. Model drafts are not ground truth.
3. Select the walls/rooms layer or the doors/windows/fixtures layer. Choose a
   class and paint with a brush, drag a rectangle, or click polygon vertices.
   Enter/Finish closes a polygon. Escape cancels it. Ctrl+Z undoes edits.
4. Zoom with +/− or Fit and use scrollbars to pan. Set overlay opacity to zero
   to inspect the source. Toggle ignored pixels to see unlabeled regions.
5. Use Ignore for unknown pixels. Background and No Icon are actual negative
   labels. Only fill remaining ignored pixels as background after reviewing
   the entire active layer. You may leave either task partially ignored.
6. Set training or validation and a building/group ID. Related views or crops
   of the same building must have the same group and partition. Identical
   source file bytes are deduplicated; other variants need manual grouping.
7. Save draft frequently. To export, check both masks, tick the review statement,
   then Save reviewed training example. Further edits clear the checkbox.

Save draft keeps working masks in `drafts/<id>/<revision>/`. Reviewed masks
are exported to `training/<id>/` as image.png, rooms.png, icons.png and meta.json.
Class maps are single-channel PNGs at original resolution. Metadata records
source hash, classes, split, group, review state, dimensions and provenance.
Older draft revisions remain on disk. Simultaneous stale-tab saves are rejected.

Saving a draft after a reviewed export invalidates that export until reviewed
again. The trainer deliberately rejects such unreviewed examples. Finish review
before training and do not edit/export while a training run is loading data.

## First training pilot

Aim for 30 independently corrected plans: 15 line drawings, 8 grey-filled,
6 renders and 1 solid-black plan where available. Assign about 6 to validation,
covering multiple styles; keep related buildings together. These are targets,
not automatically certified labels or automatically chosen independent groups.

```powershell
.venv\Scripts\python.exe scripts\finetune_cubicasa.py --data models\annotations\training --batch 1 --epochs 60
```

The trainer honors saved train/val assignments instead of randomly resplitting
them and rejects groups/source hashes crossing partitions. All examples in a
mixed dataset must have explicit split metadata if any example has it.
The existing random split remains available for older datasets without splits.
One or two labeled plans prove plumbing, not model quality. Compare held-out
wall/window/door metrics before and after fine-tuning.
