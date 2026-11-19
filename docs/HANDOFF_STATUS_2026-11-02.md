# TopoSpatial MCP — status update, 2 Nov 2026

## Status update — 2 Nov 2026 (read this first)

Supersedes parts of sections 2 and 5 below. Full contract and eval details: `docs/CANONICAL_PIPELINE.md`.

**Constraint correction.** Section 1 says not to copy decompiled Synaps code. The user has since explicitly authorised maintaining the existing kernel (`src/trace_engine/geometry_kernel.py`, AGENTS.md "Existing tracing implementation: authorized maintenance"): keep its provenance comments, do not claim it is clean-room. The restrictions on Synaps' private API and website automation still stand. The recognition model itself is not in the decompiled code (it runs server-side), so more decompiling will not close the gap.

**Built and working**
- Canonical geometry pipeline (`canonical.py`, `regularize.py`, `quality.py`, schema v2 in `models.py`): one mm frame, edge lineage, gap repair, faces, exposure, opening hosting, persistent room ids. `trace_plan` uses it for the neural engine by default (`models/neural_profile.json`: pretrained CubiCasa weights, `wall_logit_bias` 2).
- Frozen eval (`scripts/eval_trace.py`, `run_eval.bat`; manifest hash e6a6a683498b, 16 val plans). Scores walls, junctions, doors, windows, rooms, adjacency, opening hosts, invalid changes.
- Thin-wall loss weighting in `scripts/finetune_cubicasa.py` (default 250 mm threshold, weight 3). Not yet used in a real training run.
- Crop bug fixed 2 Nov: `vision.plan_region` insets its box by half its dilation, which shaved the outer walls off (twice: the network crop and the later wall clip). `pad_region` now grows the box back out. Ground-floor plan: 6 -> 10 rooms, quality 0.55 -> 0.89.
- Raster2Seq now runs: `.venv-raster2seq` got CPU torch/torchvision, `training/raster2seq_runtime/predict.py` falls back to CPU, and `raster2seq_inference._predict` reports the real error text.

**Eval results (16 frozen val plans, canonical pipeline)**

| | walls | junc. | doors | windows | rooms F1 | room IoU | adjacency | s/plan |
|---|---|---|---|---|---|---|---|---|
| CubiCasa + bias 2 (promoted) | 0.75 | 0.49 | 0.43 | 0.65 | 0.39 | 0.26 | 0.94 | 14 |
| Raster2Seq pretrained | 0.70 | 0.50 | 0.50 | 0.48 | 0.59 | 0.46 | 0.68 | 47 |
| Raster2Seq pilot30 fine-tune | 0.62 | 0.41 | 0.38 | 0.34 | 0.54 | 0.44 | 0.43 | 46 |

Raster2Seq pretrained finds far more rooms but is worse on walls/windows/adjacency. pilot30 is worse than pretrained (one eval plan was in its training set). Nothing promoted.

**Known remaining problems (traced real plans, 2 Nov)**
- Thin (about 150 mm) partitions between small rooms are merged or dropped (second plan: Bed Room 1 + Dress + Toilet came out as one room).
- Scale from printed room sizes fails when OCR names do not match: `fit_room_sizes` needs names from OCR. Passing `mm_per_px` works. Tesseract OCR garbles names ("Rol / Nory, Oilet"); labels attach to the wrong rooms.
- Stair void becomes "Space N"; bottom-right wet area/toilet merge.
- Room outlines/labels are drawn outside the rollback transaction unless `rooms_in_transaction=True`.

**Next steps, in order**
1. Hybrid: CubiCasa walls + windows, Raster2Seq room outlines/door+room labels; score on the same 16 plans.
2. Room naming from labels read by the controlling model (names, sizes, rough positions) instead of Tesseract; use them for scale.
3. Thin-partition separation before face building.
4. Align the 33 weak overlays (`dataset\gt\align_weak\index.html` -> `align_fixes.json` -> `scripts\apply_alignment.py`), then `retrain_round2.bat`; promote only if it beats the promoted row on the same manifest.
5. Plan steps 7-10 not started: graph-target exporter, junction supervision, edge-connectivity model, furniture.

**Environment gotchas**
- The MCP server loads new code only after Claude Desktop is fully quit and reopened.
- Files written to the PC from the cloud container have sometimes silently not landed: verify with a grep or file size afterwards.
- Cloud container cannot run the Windows `.venv`; heavy runs go through PowerShell (`Start-Process ... -RedirectStandardOutput logs\x.log` so closing the window does not kill them; Python buffers redirected output, so watch the output folder).
- `run_tests.bat`: kernel tests need shapely; about 52 Windows-only failures are pre-existing.
- Licence: CubiCasa5K and the Raster2Seq pretrained weights come from CubiCasa5K data (CC BY-NC 4.0). Non-commercial only.
