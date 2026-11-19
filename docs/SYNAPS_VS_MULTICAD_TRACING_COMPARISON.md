# Synaps vs multiCAD Tracing: Evidence-Based Comparison

Date: 1 November 2026

## Executive conclusion

Synaps is not winning primarily because of prettier CAD rendering or its Rust
WebAssembly kernel. It is winning at the recognition contract: its cloud
service returns one coherent architectural graph with shared wall junctions,
wall type/thickness, hosted openings, and zones already linked to boundary
cycles. The current multiCAD Raster2Seq path predicts independent room and
opening polygons at 256x256 resolution, then attempts to infer a wall graph
from coincident polygon boundaries. Recognition errors therefore become graph
fragmentation before the deterministic cleanup engine receives them.

The most valuable next model change is to train for Synaps's output contract,
not to continue fine-tuning only the existing independent polygon decoder.

## Evidence boundaries

### Observed in preserved Synaps artifacts

- Blueprint inference is invoked server-side through
  `ts.floorplan.fromImage.mutate`; no ONNX or TFLite model is present in the
  captured client assets.
- The client receives `walls_graph`, `doors_list`, `windows_list`,
  `zones_list`, global wall thicknesses, resize factors, and a normalized image
  path.
- The preserved normalized tracer response contains 36 nodes, 48 edges, 10
  doors, 15 windows, and 13 zones. The wall graph has zero degree-1 nodes and
  one connected component.
- Openings are returned with `edge_nodes`, `distance_from_lower_node`, center,
  width/length, and door type/direction. They are not attached later by a
  nearest-wall guess.
- Zones contain both semantic text/center and explicit `node_ids`/`edge_ids`
  describing their boundary.
- The client sends the returned walls into the Rust/WASM `WallPlanarEngine`,
  whose exposed interface solves room faces, wall exposure, and corner fills.

Primary evidence:

- `Synaps/engine-evidence/modal-hydra-normalized-tracer-response-C6Econpk.js`
- `Synaps/engine-evidence/_modelId-Bg8qugoQ.js`
- `Synaps/engine-evidence/index-pYztK0Xm.js`
- `Synaps/engine-evidence/Provider-7j2L3WCo.js`
- `Synaps/engine-evidence/realtime_store_bg-DnFck_2s.wasm`
- `Synaps/engine-evidence/EXCERPTS.md`

### Inferences

- The server model is probably multi-task and graph-aware, or is followed by a
  substantial server-side graph reconstruction stage. The captured response
  proves the output behavior, but not the hidden network architecture.
- The server likely performs higher-resolution and/or tiled inference because
  it returns a separately normalized image and independent resize factors. The
  exact inference tensor size is not exposed.
- Synaps's perfect results on the user's drawings may include server-side model
  improvements not represented in the captured client bundles.

### Unknowns

- Backbone, training dataset size, loss functions, and exact model weights.
- Whether wall graph construction is neural, deterministic, or hybrid on the
  server.
- Confidence thresholds, test-time augmentation, and failure-retry policy.

## Pipeline comparison

| Stage | Synaps | Current multiCAD | Consequence |
|---|---|---|---|
| Input normalization | Cloud creates and stores a normalized image and returns separate resize factors | Known brochure crop, then Raster2Seq resize/pad to 256x256 | Cropping helped, but 256px still destroys thin openings and double-wall detail |
| Primary prediction | Architectural wall graph plus hosted openings and zones | Independent room polygons and opening segments | Our wall topology is indirect and fragile |
| Wall representation | Shared nodes, typed edges, per-edge thickness | Room polygon boundaries are deduplicated into edges | Slight polygon disagreement creates duplicate/disconnected walls |
| Openings | Host edge IDs and scalar offset are model/service outputs | Nearest-wall geometric hosting after polygon prediction | Wrong or fragmented host walls lose or clamp openings |
| Rooms | Zone explicitly includes boundary node/edge cycle | Face traversal attempts to recover cycles, then assigns a seed | Broken wall graph means missing/extra rooms despite good room IoU |
| Geometry repair | WASM room/exposure/fill passes on one coherent body graph | Python snapping, splitting, collinear merge, face recovery | Algorithms are broadly comparable, but ours receives poorer topology |
| Thickness | Per-edge predicted thickness plus internal/external globals | One nominal thickness, type inferred from duplicate boundaries | Less faithful external envelopes and partitions |
| Furniture | Separate OBB model and room containment | Not part of current Raster2Seq production trace | Missing secondary semantic correction signal |

## Quantitative topology comparison

The preserved Synaps response is not the same drawing as the local unseen
benchmark, so this is a structural comparison rather than an accuracy
head-to-head.

| Output | Nodes | Edges | Dead ends | Components |
|---|---:|---:|---:|---:|
| Synaps preserved real response | 36 | 48 | 0 | 1 |
| multiCAD pretrained, cropped unseen 1 | 57 | 58 | 5 | 8 |
| multiCAD best, cropped unseen 1 | 65 | 64 | 9 | 10 |
| multiCAD pretrained, cropped unseen 2 | 70 | 70 | 2 | 12 |
| multiCAD best, cropped unseen 2 | 67 | 61 | 14 | 11 |

The component count is the clearest difference. A building can contain many
room cycles while its structural wall graph remains one connected arrangement.
Our model currently emits nearly one disconnected component per room because
the graph is reconstructed from independently decoded polygons.

## Current model findings

- Fine-tuned best improves the reference room IoU from 89.27% to 92.08% and
  recovers all 12 reference rooms.
- It worsens opening localization and produces more dead ends on unseen plans.
- The latest checkpoint is collapsed and correctly quarantined by the runtime
  topology gate.
- Cropping improved opening detection, but it did not repair the fundamental
  representation mismatch.
- Only 30 plans have pseudo/annotation data. The 233 cropped images are not 233
  supervised examples.

## Recommended target architecture

### 1. Replace the training target

Train a shared-node wall graph rather than deriving walls solely from room
polygon boundaries. The target should contain:

- canonical junction coordinates;
- typed wall edges with thickness;
- room cycles expressed as ordered edge IDs;
- doors/windows with host edge and normalized scalar offset;
- room class/text seed;
- optional furniture OBBs.

Room polygons can remain an auxiliary output and loss, but should no longer be
the sole source of structural walls.

### 2. Raise effective inference resolution

Use at least 512-1024px plan inference or overlapping tiles with a global graph
merge. A 256px canvas gives a 100mm wall only roughly 1-2 pixels on many of the
brochure plans and makes small windows/doors quantize to the same bins.

### 3. Add topology losses and constraints

- junction heatmap/keypoint loss;
- endpoint-to-junction assignment loss;
- edge existence/type/thickness loss;
- room-cycle closure loss;
- opening host-edge classification plus scalar-offset regression;
- penalties for degree-1 nodes except legitimate exterior wall ends;
- penalties for disconnected components and duplicate parallel boundaries.

### 4. Use deterministic geometry after graph prediction

Keep the existing `graph.clean`, planar noding, room traversal, opening
re-hosting, and Synaps-derived exposure/fill ideas. They are valuable, but they
should regularize an already shared graph instead of synthesizing one from
inconsistent room polygons.

### 5. Build paired Synaps-output evaluation data lawfully

Where permitted by the user's account and Synaps terms, export the user's own
trace results for the same plans and compare graph-level metrics:

- node/edge precision within metric tolerances;
- connected components and degree histogram;
- room cycle IoU and closure;
- opening host-edge accuracy and scalar offset;
- thickness/type accuracy.

Do not treat Synaps output as ground truth without manual review; teacher errors
and OCR misspellings are visible even in the preserved response.

## REA connection status

The preserved static REA analysis is valid and identifies Evidence ID
`ev_7dbade7839464142c49fea8a82f9fd223f136d0a5de012a3bc76ba846af75d02`.
Live REA MCP tools were not available in the current Codex session because
`C:\Users\LENOVO\.codex\config.toml` has no REA registration. The read-only
setup plan proposes adding only the version-pinned
`npx -y rea-agents@6.7.0 mcp` registration, preserving unrelated config and
backing up the file to `config.toml.rea.backup`; applying it requires explicit
approval and a Codex restart.
