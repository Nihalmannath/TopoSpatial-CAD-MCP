# Ground truth from IFC exports

Training labels come from the per-project IFC exports in
`dataset/raw_vector_exports/PDF plan/<folder>/`: each folder holds plan
images and one or more `.ifc` files (one storey per traced plan). The old
PDF-scraping scripts are in `scratch/deprecated/`; their output moved to
`dataset/_deprecated_ground_truth_cad/` and should not be used.

## Run it

From the repo root, in the project venv (needs only numpy, Pillow, OpenCV and
pydantic; scipy is used when present):

```powershell
.venv\Scripts\python.exe scripts\build_gt_dataset.py build
# or a few folders:  build --folders "1 FIRST 6" "3 THIRD 23"
```

Then open `dataset\gt\review.html`. Every candidate shows its overlay:
red = walls, orange = railings, blue = windows, green = doors, purple =
room names. `auto_ok` candidates are pre-ticked. Untick anything wrong,
tick good `review` ones, click **download accepted.txt**, then:

```powershell
.venv\Scripts\python.exe scripts\build_gt_dataset.py accept --list accepted.txt
# or, without reviewing:  accept --auto   (every auto_ok candidate)
.venv\Scripts\python.exe scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 --crop 512 --batch 4
```

`accept` copies samples to `dataset\gt\training\`, sets
`annotation_complete: true`, and splits by folder (every 5th folder is
validation unless `--val-groups` says otherwise). The same image appearing
in two folders is kept once.

## What each step does

1. **IFC reader** (`trace_engine/ifc_step.py`, `ifc_groundtruth.py`, no
   ifcopenshell needed): per storey, wall footprints (the hull of all body
   pieces, since walls are split around openings) become centrelines with
   thickness; ends are extended to the wall they abut; `graph.clean` nodes
   T-junctions; room outlines with no wall behind them (open plan, drawn
   dividers) become `separator` edges; openings are hosted on their wall via
   `IfcRelVoidsElement`/`IfcRelFillsElement`; spaces give room names.
2. **Registration** (`trace_engine/register.py`): long straight dark strokes
   are the image evidence (hatching, furniture and text filtered out). The
   storey is fitted in the image-frame pose the exporter uses (no rotation,
   y down the image), and in all 8 poses only when that finds nothing. Fit:
   template search over scale and position, then point-to-line ICP.
3. **Matching**: every image is scored against every storey in its folder
   and assigned 1:1. Images with no storey (renders, unrelated plans) stay
   unassigned.
4. **Samples** (`trace_engine/gt_dataset.py`): `image.png`, `rooms.png`,
   `icons.png`, `ignore.png`, `graph.json` (mm and px graph with junction
   types and hosted openings, for graph training), `overlay.png`,
   `meta.json`.

## Buckets

| Bucket | Rule | Meaning |
| --- | --- | --- |
| `auto_ok` | support >= 0.55, score >= 0.18, margin >= 0.08, usual pose | Safe to train on without review |
| `review` | combined match >= 0.30 | Probably right; check the overlay |
| `reject` | otherwise, or not a line drawing | Not written unless `--write-rejects` |

*support* = share of wall faces drawn along most of their length; *score* =
line precision/recall F-measure normalised against an off-register shift;
*margin* = lead over the next-best storey. Calibrated on hand-checked pairs:
correct fits had support 0.42-0.80, wrong ones 0.20-0.50. Upper floors of
the same house share outer walls with the ground floor, so a ground-floor
image whose own storey is missing can fit an upper floor; those land in
`review`, which is why review matters.

## Known limits

- Door swing is not in the IFC; doors default to hinge start / swing left.
- Labels are the exporter's trace, not hand-checked truth: reject overlays
  where rooms or openings are visibly wrong.
- Folders whose images outnumber their storeys (e.g. `10 TEN 7`) are mostly
  unrelated images; expect few matches there.
