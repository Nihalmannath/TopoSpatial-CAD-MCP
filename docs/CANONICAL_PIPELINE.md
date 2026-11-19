# Canonical geometry pipeline

Status (2 Nov 2026): implemented behind `geometry_pipeline="canonical"`. The
legacy path stays the default until the gates at the end of this page pass.

```
recogniser (classic | neural | raster2seq | geometry_kernel)
   -> TraceResult (px or mm, schema v2)
   -> canonical.normalize           one transform into mm, y-up
   -> regularize.regularize_trace   cleanup + lineage, gap repair, faces, exposure,
                                    thickness, opening hosting, rooms, diagnostics
   -> quality.assess                observable indicators (engine choice)
   -> compiler.compile_regularized  manage_design changes -> preview -> apply
```

## Contract (`models.py`, schema v2)

All v1 payloads still validate. New optional fields:

| Field | Meaning |
|---|---|
| `TraceEdge.id`, `TraceOpening.id` | stable ids; `upgrade_trace` fills `e<i>` / `d<i>` / `w<i>` |
| `confidence`, `provenance` | detection confidence 0..1 and where the entity came from |
| `TraceEdge.thickness_source` | `measured` (default), `user_supplied`, `default` (nominal; may be replaced by exposure) |
| `TraceResult.frame` | `origin`, `calibrated`, `scale_source` |
| `TraceResult.diagnostics` | structured notes (`severity`, `code`, `message`, `entity`, `location`) |
| `TraceResult.furniture_list` | oriented furniture boxes (only when furniture output is enabled) |

Opening position: `distance_from_lower_node` (= `center_offset`) is the distance
from `edge_nodes[0]` to the opening **centre**; CAD's `offset` is the distance to
the opening **start** (`start_offset = center_offset - width / 2`). `hinge` /
`swing` are relative to `edge_nodes[0] -> edge_nodes[1]` in y-up space.

## Frame (`canonical.py`)

`Frame(mm_per_unit, y_down, origin)`: uniform scale, translation and Y inversion
only. Anisotropic scale is refused (`geometry_kernel.CoordinateProjector` needs
`allow_anisotropic=True`). Every tolerance after `normalize` is millimetres.

## Regularization (`regularize.py`)

- Cleanup reuses `graph.py` steps, which now carry lineage: each wall has
  `sources` (input edge ids). `edge_lineage[edge_id]` gives the intervals of the
  resulting walls that each input edge covers; `node_map` maps input nodes.
- Wall ids are deterministic from lineage: `e3`, split pieces `e3.1`, `e3.2`,
  merged walls `e3+e7`, repairs `...~gap`.
- Conflicts are never silent: duplicate walls with different explicit
  thicknesses (`duplicate_wall_thickness_conflict`), straight runs with different
  explicit thicknesses are kept apart (`colinear_kept_apart`).
- Gap repair (only dangling ends, limit `gap_repair_mm`, default
  clamp(1.5 x median thickness, 150, 500) mm, below any doorway):
  1. extend a wall straight on to the wall it meets, or into a collinear wall end
     ahead; 2. extend two ends to their common L corner; 3. otherwise join two
  ends with a separator. Repeats until nothing changes (max 4 rounds). Every
  repair is a diagnostic (`gap_extended`, `gap_corner`, `gap_joined`).
- Faces: half-edge traversal (`rooms.find_faces`). Exposure:
  `geometry_kernel.solve_wall_exposure` (5 probes per wall; walls whose exposure
  changes are split at the change). Faces labelled as outdoor (balcony,
  courtyard, terrace, ...) count as outside.
- Thickness: measured / user values are kept; `default` thickness and type are
  set from exposure (internal 150 mm, external 300 mm, configurable).
- Openings: host by the reported edge's lineage first; bounded geometric
  fallback (angle <= 10 deg, distance <= 2 x wall thickness, <= 400 mm) with an
  ambiguity warning between parallel walls. Wider-than-wall, no host and
  overlaps are rejected with a diagnostic; clamping is reported.
- Rooms: clear interior = centerline face inset by half thickness. Labels by
  containment; boundary-adjacent and multiple labels are flagged; unlabelled
  rooms stay unclassified (compiled as "Space N"). Label point is inside
  concave rooms. `geometry_hash` is canonical (winding / start / 1 mm); the
  persistent `room_id` survives shape edits when `previous_rooms` is passed.

## Geometry kernel (`geometry_kernel.py`)

Maintained under AGENTS.md "Existing tracing implementation: authorized
maintenance"; its provenance comments are unchanged. Fixes (2 Nov 2026): one mm
frame in `PlanGeometryTracer.trace` (rooms and exposure previously used
different frames), `snap_tol` in mm, Y inversion, no input mutation, door
type/hinge/swing kept, multi-probe exposure with `unknown` / `mixed`, measured
thickness kept, label point inside concave rooms, lazy shapely import. The
canonical pipeline uses its exposure solver, thickness defaults and FNV hash.
`clean_walls_graph` is still used by `PlanGeometryTracer`; the canonical path
uses the lineage-aware `graph.py` cleanup instead (one service per pipeline).

## MCP (`trace_plan`)

- `geometry_pipeline="legacy" | "canonical"` (default legacy).
- Engine choice uses `quality.assess` (dangling ends, enclosed area, openings
  placed, overlaps, agreement with `room_sizes`, short walls) instead of the
  room count. The result reports `geometry_pipeline`, `calibration`, `quality`,
  `diagnostic_counts`, `diagnostics`.
- `rooms_in_transaction=True` puts room boundaries in the same manage_design
  transaction as the walls (rollback removes them). Default still draws
  outline polylines afterwards; that drawing is now all-or-nothing.
- The server must be fully restarted to load the new code.

## Evaluation (`scripts/eval_trace.py`)

```
run_eval.bat --freeze-manifest          (once: freezes dataset\gt\eval_manifest.json)
run_eval.bat                            (classic, pretrained, every finetuned checkpoint x raw/legacy/canonical)
run_eval.bat --wall-bias 0,0.5,1 --stages canonical
```

Metrics: walls, junctions, doors / windows / any opening, opening width error
and correct-host rate, rooms (compiled clear polygons) with boundary error and
adjacency, invalid changes, rejected entities, runtime. `summary.json` records the
manifest hash and tolerances, so two runs are only compared when they match.

Stress test on the 16 val plans (IFC geometry with every wall end shortened at
random, the typical neural failure), rooms found out of 161:

| ends shortened up to | legacy | canonical |
|---|---|---|
| 0 mm | 152 | 153 |
| 150 mm | 85 | 123 |
| 300 mm | 2 | 67 |

## Promoted neural settings (`models/neural_profile.json`)

`trace_plan` reads this file: the neural engine uses its `weights` (ahead of
any fine-tuned checkpoint), its `wall_logit_bias` and its `geometry_pipeline`.
Explicit `weights_path` / `wall_logit_bias` / `geometry_pipeline` arguments
still win. Promoted 2 Nov 2026 from the frozen eval (16 val plans):

| Pretrained CubiCasa, canonical | bias 0 | **bias 2 (promoted)** | bias 3 | bias 4 |
|---|---|---|---|---|
| Walls F1 | 0.72 | 0.75 | 0.76 | 0.72 |
| Doors F1 | 0.30 | 0.49 | 0.51 | 0.48 |
| Rooms F1 | 0.24 | 0.38 | 0.40 | 0.39 |

Bias 3 is within noise of 2 but less stable per plan; bias 4 starts to break
plans. Round-1 fine-tuned weights peaked at rooms F1 0.28 (bias 1). The eval
always scores the promoted setting as `promoted+b<bias>`; replace the profile
only when a new model beats it on the same manifest.

## Findings, 2 Nov 2026

- **Crop fix.** `plan_region` insets its box by half its dilation. That cut the outer walls off in two places (network crop, then the wall clip before run extraction). `vision.pad_region` grows the box back out (network crop: max(24 px, 4 x thickness); clip: 2 x thickness). Ground-floor plan: 6 -> 10 rooms, quality 0.55 -> 0.89.
- **Raster2Seq vs CubiCasa** (16 frozen val plans, canonical): see the table in `HANDOFF_TRACING.md`. Raster2Seq pretrained: rooms F1 0.59 vs 0.39, walls 0.70 vs 0.75, windows 0.48 vs 0.65, adjacency 0.68 vs 0.94, 47 s vs 14 s per plan. The pilot30 fine-tune is worse than the pretrained checkpoint. Run with `run_eval_r2seq.bat` / `--raster2seq`. Raster2Seq traces are in mm; the eval rescores them in pixels.
- **Open**: thin partitions, label-to-room assignment, scale from printed sizes when OCR fails, hybrid CubiCasa walls + Raster2Seq rooms.

## Promotion gates (legacy -> canonical default)

Set the thresholds after the first real baseline on the frozen manifest; then
require, for the best neural model:

1. no metric worse than legacy by more than noise (inspect every per-plan regression);
2. rooms F1 and junction F1 better than legacy;
3. 0 invalid changes; no opening lost without a diagnostic;
4. a live `trace_plan` preview + apply + rollback on two plans in AutoCAD.

Not done yet (plan steps 7-10): graph-target exporter, junction supervision,
edge-connectivity model, furniture output.
