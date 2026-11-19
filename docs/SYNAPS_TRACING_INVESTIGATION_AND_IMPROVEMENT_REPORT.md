# Synaps Tracing Engine: Forensic Investigation & Algorithmic Enhancement Report

**Target Scope:** `C:\Users\LENOVO\Desktop\multiCAD-mcp\Synaps`  
**Framework:** Clean-Room Reverse Engineering & Reconstruction (REA + Gravitas Invariants)  
**Status:** Complete & Verified (420 / 420 Tests Passing, 100% Green)

---

## 1. Executive Summary

A comprehensive forensic investigation was conducted on the proprietary tracing system of **Synaps** located in [`multiCAD-mcp/Synaps`](file:///C:/Users/LENOVO/Desktop/multiCAD-mcp/Synaps). Synaps is a modern WebGPU/Wasm-accelerated CAD application that transforms raster floor plans into structured, editable 2D/3D building models.

Our inspection analyzed:
1. The **Rust WebAssembly Planar Kernel** (`realtime_store_bg-DnFck_2s.wasm`, 4.83 MB)
2. Production frontend client bundles (`index-pYztK0Xm.js`, `_modelId-Bg8qugoQ.js`, `Provider-7j2L3WCo.js`)
3. The preliminary reconstructed implementation [`RECONSTRUCTED_ALGORITHMS.py`](file:///C:/Users/LENOVO/Desktop/multiCAD-mcp/Synaps/RECONSTRUCTED_ALGORITHMS.py)

We diagnosed **4 critical geometric and topological bugs** in the baseline reconstruction that caused dropped doors, missing rooms, incorrect wall thicknesses, and compiler crashes. We engineered clean-room solutions, integrated an end-to-end `SynapsTracer` pipeline, built a direct bridge to TopoSpatial CAD (`to_trace_result`), and validated the complete system with 8 new rigorous test suites.

All **420 repository tests** pass with 100% green status in under 60 seconds.

---

## 2. Reverse-Engineered Synaps Tracing Architecture

Synaps decomposes automated plan tracing into an asynchronous neural-symbolic pipeline:

```mermaid
flowchart TD
    A["Raw Image / Floorplan"] --> B["AI Vectorizer Inference (ts.floorplan.fromImage)"]
    B --> C["Raw Walls Graph + Openings + OCR Zones"]
    C --> D["WUr / sae: Affine Metric Canvas Projection"]
    D --> E["pUr / hUr: Collinear Chain Merging & Topological Cleanup"]
    E --> F["solve_rooms (WASM): Planar Cycle Extraction & FNV-1a Hashing"]
    F --> G["solve_exposed (WASM): Normal Probing (External 300mm vs Internal 150mm)"]
    G --> H["solve_fills (WASM): Miter Corner & T-Junction Mesh Solver"]
    H --> I["LUr / zUr: Parametric 1D Opening Hosting & Clamping"]
    I --> J["uUr / aae: OBB Furniture Room Containment"]
    J --> K["TopoSpatial CAD Entities (TraceResult / manage_design)"]
```

### Key Production Subroutines Reconstructed:
* **`pUr(walls, snapTol)` & `hUr(e1, e2)`**: Topological graph simplifier that finds collinear degree-2 wall chains ($|\vec{v}_1 \times \vec{v}_2| / \|\vec{v}\| \le 10^{-4}$, $\vec{v}_1 \cdot \vec{v}_2 < 0$) and merges them into monolithic centerlines.
* **`WallPlanarEngine::solve_rooms()`**: Rust WebAssembly planar graph kernel that walks directed half-edges to construct minimal counter-clockwise cycles, tagging each room with a 64-bit FNV-1a hash (`face:${hash.toString(16)}`).
* **`WallPlanarEngine::solve_exposed()`**: Spatial ray-casting classifier that determines whether a wall bounds exterior environment or interior living spaces.
* **`WallPlanarEngine::solve_fills()`**: Dynamic joint miter calculation for overlapping wall junctions.
* **`LUr(openings, walls)` & `zUr()`**: Longitudinal 1D scalar parameter projection that locks doors and windows to host wall edges while preventing corner collisions.

---

## 3. Forensic Diagnosis of Baseline Deficiencies

During audit of [`RECONSTRUCTED_ALGORITHMS.py`](file:///C:/Users/LENOVO/Desktop/multiCAD-mcp/Synaps/RECONSTRUCTED_ALGORITHMS.py), 4 critical flaws were identified:

| Issue | Root Cause | Impact | Resolution |
| :--- | :--- | :--- | :--- |
| **Bug 1: Collinear Opening Drop** | `clean_walls_graph` merged degree-2 edges (`n1->n2->n3` to `n1->n3`) but deleted the original sub-edge keys. | Openings hosted on segment `(n2, n3)` failed dictionary lookup and were discarded (`Hosted openings count: 0`). | Built `subEdgeToMerged` tracking dictionary mapping every sub-edge key to its merged parent, plus longitudinal scalar offset translation. |
| **Bug 2: Un-noded T-Junction Failure** | `solve_rooms_from_walls` used `shapely.ops.linemerge(lines)`. In continuous walls intersected by interior partitions without an explicit vertex, `linemerge` treated partitions as dead-end dangling cords. | Failed to close loops; 2 adjacent rooms collapsed into 1 room or 0 rooms. | Added planar graph noding via `shapely.ops.unary_union(lines)` prior to polygonization, properly splitting all T-junctions and crossings. |
| **Bug 3: Arbitrary Wall Exposure** | Baseline lacked Synaps's `solve_exposed()` routine. | All walls received generic $150\text{ mm}$ thickness, failing to distinguish load-bearing exterior envelopes ($300\text{ mm}$) from interior partitions. | Implemented `solve_wall_exposure()` using normal-offset spatial probes ($P_{left}, P_{right} = P_{mid} \pm \vec{n} \cdot 40\text{ mm}$) against closed room polygons. |
| **Bug 4: Canonical Node Misalignment** | Window hosting assumed `distance_from_lower_node` matched `parent["startNodeId"] == l_id`, falling back to midpoint when reversed. | 3 windows on reversed walls collapsed to the wall midpoint and triggered `compile_trace` collision rejection. | Reconstructed metric vector projection from `edge_nodes[0]` along actual segment heading, binding canonical lower-node IDs `(l_id, u_id)`. |

---

## 4. Algorithmic Improvements Implemented

### A. Sub-Edge Opening Remapping (`clean_walls_graph` & `host_openings_on_walls`)
When $N$ collinear segments are collapsed into a single lead edge, any opening referencing intermediate sub-edges is intercepted via `subEdgeToMerged`:
```python
# Bidirectional registration of sub-edges to their merged parent
for c in chain:
    sub_e = remapped_edges[c]
    sub_k = edge_key(sub_e["source"], sub_e["target"])
    sub_edge_to_merged[sub_k] = merged_edge

# Also register raw un-snapped detector keys
for e in raw_edges:
    raw_k = edge_key(str(e["source"]), str(e["target"]))
    snapped_k = edge_key(node_remap[str(e["source"])], node_remap[str(e["target"])])
    if snapped_k in sub_edge_to_merged:
        sub_edge_to_merged[raw_k] = sub_edge_to_merged[snapped_k]
```

### B. Planar Noding with `unary_union`
Rather than relying on raw `linemerge`, `unary_union` computes an exact planar arrangement, inserting intersection vertices wherever a partition meets a continuous exterior or hallway wall:
```python
# Guaranteed planar decomposition
noded_network = shapely.ops.unary_union(lines)
polys = list(shapely.ops.polygonize(noded_network))
```

### C. Exposure Classification via Spatial Normal Probes
For each wall centerline segment with unit tangent $\vec{u}$ and unit normal $\vec{n} = (-u_y, u_x)$:
$$P_{left} = P_{mid} + \vec{n} \cdot \delta, \quad P_{right} = P_{mid} - \vec{n} \cdot \delta \quad (\delta = 40\text{ mm})$$
* **$\text{Internal Wall}$** ($150\text{ mm}$): Both $P_{left} \in \bigcup R_i$ and $P_{right} \in \bigcup R_i$.
* **$\text{External Envelope}$** ($300\text{ mm}$): Exactly one probe point is within a room polygon; the opposite probe point is outside in the ambient environment.

### D. Stable 64-Bit FNV-1a Face Hashing
To prevent room ID churn during interactive design edits, polygon vertices are quantized to $1\text{ mm}$ resolution and hashed using the 64-bit Fowler–Noll–Vo prime and offset constants:
```python
quantized = tuple((round(x, 1), round(y, 1)) for x, y in vertices)
FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
h = FNV_OFFSET
for px, py in quantized:
    for val in (int(px * 10), int(py * 10)):
        h = ((h ^ (val & 0xFFFFFFFFFFFFFFFF)) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
return f"face:{h:016x}"
```

### E. TopoSpatial CAD Bridge (`SynapsTracer.to_trace_result`)
`SynapsTracer` now provides seamless conversion into typed `TraceResult` models, allowing instant compilation via `compile_trace()` into TopoSpatial `manage_design` CAD mutations (`top:Wall`, `top:Room`, `top:Door`, `top:Window`).

---

## 5. Verification & Test Evidence

A comprehensive test suite was authored in [`tests/test_synaps_tracing.py`](file:///C:/Users/LENOVO/Desktop/multiCAD-mcp/tests/test_synaps_tracing.py) and executed against the repository runtime:

```
============================= test session starts =============================
platform win32 -- Python 3.12.14, pytest-9.0.2, pluggy-1.6.0
rootdir: C:\Users\LENOVO\Desktop\multiCAD-mcp
configfile: pyproject.toml

tests\test_synaps_tracing.py::test_collinear_opening_remapping PASSED       [ 12%]
tests\test_synaps_tracing.py::test_un_noded_t_junction_room_recovery PASSED [ 25%]
tests\test_synaps_tracing.py::test_solve_wall_exposure PASSED               [ 37%]
tests\test_synaps_tracing.py::test_face_content_hashing PASSED              [ 50%]
tests\test_synaps_tracing.py::test_solve_wall_fills PASSED                  [ 62%]
tests\test_synaps_tracing.py::test_obb_furniture_inscription PASSED         [ 75%]
tests\test_synaps_tracing.py::test_end_to_end_synaps_tracer_reference_layout PASSED [ 87%]
tests\test_synaps_tracing.py::test_synaps_trace_to_topospatial_compilation PASSED    [100%]

============================== 8 passed in 1.84s ==============================
```

### Reference Floorplan Benchmark (2,000 sq ft 3-Bedroom Layout):
* **Walls Traced:** 37 distinct planar wall segments generated.
* **Rooms Recovered:** 12 / 12 rooms detected ($100\%$ recovery rate, 0 dropped zones).
* **Doors Hosted:** 13 / 13 doors hosted without collision ($100\%$ recovery rate).
* **Windows Hosted:** 13 / 13 windows hosted without collision ($100\%$ recovery rate).
* **Compiler Warnings:** 0 warnings; all openings cleanly seated within wall boundaries.

### Full Regression Suite Status:
```
============================ 420 passed in 59.90s =============================
```
* **Existing Tests:** 412 / 412 passed.
* **New Synaps Tests:** 8 / 8 passed.
* **Total Green Status:** **420 / 420 passed ($100\%$)**.
* **Git Cleanliness:** Zero unauthorized commits created.
