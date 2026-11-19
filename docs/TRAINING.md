# Local segmentation training

The 233 source images are not supervised training examples until labeled.
The default `verify_labeling_plan33` export is an alignment fixture with one
wall, not a complete annotated floor plan. Do not train on it as ground truth.

Each sample directory requires RGB `image.png`, single-channel `rooms.png`
(IDs 0–11), and single-channel `icons.png` (IDs 0–10), with identical dimensions.
Preserve class indices; do not save color previews as labels.

Review both label maps against the input. Unannotated pixels must be 255 in
the corresponding map, or nonzero in an optional single-channel `ignore.png`
of the same size (which ignores both tasks). Only after review, set
`"annotation_complete": true` in `meta.json`. This means every nonignored
pixel is correctly annotated, including background; it does not require
every pixel to be labeled. The trainer refuses unreviewed examples.

Use independent plans for validation. Keep alternate exports/crops of the
same building in one partition; the current random split is by sample folder,
so pre-deduplicate and use one sample per building. Recorded train/validation
IDs and seed accompany saved weights. One sample can exercise training but
cannot establish generalization. Classes absent in both prediction and truth
are reported as null rather than zero IoU.

Run from the repository:

```powershell
.venv\Scripts\python.exe scripts\finetune_cubicasa.py --smoke --batch 1
.venv\Scripts\python.exe scripts\finetune_cubicasa.py --data PATH_TO_REVIEWED_DATA --epochs 60 --batch 1
```

Smoke uses synthetic labels, performs two optimization steps, and saves nothing.
Real runs save the best validation model, or the latest epoch when validation
is unavailable. `--base` loads model weights only: it is not an optimizer-state
resume. Room/icon losses do not supervise the 21 junction heatmap channels;
fine-tuned weights are intended for the existing segmentation-only consumer.
Full-image validation may require substantial GPU memory on large plans.
