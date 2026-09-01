# 02 - System Architecture

## Overview

```
┌────────────────────────────────────────────────────────┐
│  TopoSpatial-CAD MCP Server (server.py)                │
│  9 unified tools + local orchestration + topology      │
└──────────────┬─────────────────────────────────────────┘
               │
       ┌───────┴──────────┐
       │                  │
       ▼                  ▼
┌──────────────┐  ┌──────────────┐
│ Tools Layer  │  │ Config       │
│ (mcp_tools/) │  │ (core/)      │
└──────┬───────┘  └──────────────┘
       │
       ▼
┌────────────────────────────────────┐
│  AutoCADAdapter (Mixin Composition)│
│  ├── UtilityMixin                  │
│  ├── ConnectionMixin               │
│  ├── DrawingMixin                  │
│  ├── LayerMixin                    │
│  ├── ArchitectureMixin             │
│  ├── ... (12 mixins total)         │
│  └── CADInterface (ABC)            │
└────────────────┬───────────────────┘
                 │
    ┌────────────┼────────────┬───────────────┐
    │            │            │               │
 AutoCAD      ZWCAD      GstarCAD      BricsCAD
    │            │            │               │
    └────────────┼────────────┴───────────────┘
                 ▼
        Windows COM Layer (pywin32)
```

---

## Components

### 1. Server (`server.py`)

Entry point that registers **9 unified MCP tools** via FastMCP. Seven tools
dispatch established CAD commands, `manage_topology` preserves the direct graph
workflow, and `manage_design` provides the preferred high-level architectural
lifecycle.

### 2. Tools (`mcp_tools/tools/`)

9 modules, each providing one unified tool or workflow:

| Module | Tool | Actions | Responsibility |
| :--- | :--- | :--- | :--- |
| `design.py` | `manage_design` | 11 | Inspect/context, local plans, compact preview, apply/cancel/rollback, metrics |
| `topology.py` | `manage_topology` | 5 | Analyze, query, preview, apply, export ontology |
| `session.py` | `manage_session` | 12 | Connection, capabilities, view, capture, history, dashboard |
| `drawing.py` | `draw_entities` | 11 | Unified parameterized entity creation |
| `blocks.py` | `manage_blocks` | 6 | Block management & attribute tags |
| `layers.py` | `manage_layers` | 9 | Layer management & queries |
| `files.py` | `manage_files` | 6 | File operations, format conversion, safe Recycle Bin cleanup |
| `entities.py` | `manage_entities` | 10 | Select, move, rotate, scale, color |
| `export.py` | `export_data` | 2 formats × 2 scopes | Data extraction & formatted Excel |

### 3. Adapter Layer (`adapters/`)

Uses a mixin-based composition architecture:

```python
class AutoCADAdapter(
    UtilityMixin,
    ConnectionMixin,
    DrawingMixin,
    LayerMixin,
    FileMixin,
    ViewMixin,
    SelectionMixin,
    EntityMixin,
    ManipulationMixin,
    BlockMixin,
    ExportMixin,
    ArchitectureMixin,
    CADInterface,
):
    """Composite adapter combining all mixin capabilities."""
```

Each mixin handles a single domain:
- **ConnectionMixin**: Connects to running CAD, manages document lifecycle
- **DrawingMixin**: Creates geometry (lines, circles, arcs, text, tables)
- **LayerMixin**: Layer operations (create, toggle, rename, lock, color)
- **FileMixin**: Save, export (DWG, DXF, PDF), switch drawings
- **ViewMixin**: Zoom, undo/redo, DPI-aware unobscured window capture, and
  command-based view export where the CAD product supports it
- **SelectionMixin**: Entity selection by type, layer, handle, color
- **EntityMixin**: Query properties, extract bounding boxes
- **ManipulationMixin**: Move, rotate, scale, copy, delete, change color/layer
- **BlockMixin**: List, query, insert, create blocks, manage attributes
- **ExportMixin**: Extract structured data, export to formatted Excel
- **ArchitectureMixin**: Discovers the installed ACA automation API and creates
  native walls, doors, windows, and opening-to-wall anchors
- **UtilityMixin**: Color conversion, coordinate validation, helpers

### 4. Adapter Manager (`adapters/adapter_manager.py`)

- **`AdapterRegistry`**: Process-wide registry with thread-local adapter state
- **`get_adapter(cad_type)`**: Returns adapter for requested or active CAD
- **Auto-detection**: Probes running CAD processes and connects automatically

COM application/document proxies are never shared between MCP worker threads,
the dashboard, or topology analysis. Each caller resolves a local proxy for its
own COM apartment.

### 5. Core Abstractions (`core/`)

- **`CADInterface`**: Abstract base class defining CAD contracts
- **`ConfigManager`**: Singleton with cascading config search
- **`TopoSpatialError`**: Comprehensive exception hierarchy

### 6. Spatial Topology Engine (`topology_engine/`)

The topology layer receives immutable, plain-coordinate snapshots from the CAD
bridge. COM entities never leave the calling thread. A dedicated single-worker
analysis executor runs TopologicPy/Shapely geometry using the verified
`topologic_core` backend.

It builds explicit semantic nodes from `TOPOSPATIAL_TOPOLOGY` XData, reports
untagged enclosed areas as candidates, derives spatial relationships, validates
strict change documents, and stores expiring preview transactions. Candidate
polygons that duplicate an explicitly managed clear room are suppressed even
when native wall centerlines create a slightly larger envelope.

Representation selection is capability-based and occurs during preview. `auto`
selects native `AecDbWall`, `AecDbDoor`, and `AecDbWindow` objects when AutoCAD
Architecture and a compatible native wall host are available; otherwise it selects
the standard-entity adapter. Explicit `native_aec` requests fail safely during
preview when those requirements are not met. The resolved value is stored in the
transaction and XData, so apply never makes a different representation decision.

### Topology transaction sequence

```text
MCP worker             Analysis worker                AutoCAD / filesystem
    │                         │                                  │
    ├─ snapshot coordinates ─►│ analyze + graph                  │
    │◄─ revision/candidates ──┤                                  │
    ├─ preview/validate ──────►│ plan operations                 │
    │◄─ transaction + diff ───┤                                  │
    ├─ apply/recheck revision ──────────────────────────────────►│
    │                         │◄─ new coordinate snapshot ───────┤
    │                         ├─ post-apply graph validation     │
    │                         └─────────────────────────────────►│ sidecars
    │◄─ result + new revision ───────────────────────────────────┤
```

Apply defers viewport refresh until post-validation completes. All mutations are
grouped by AutoCAD undo marks. If mutation or post-validation fails, the bridge
issues one grouped undo and waits until the preview revision is observable
again. Sidecars are atomically replaced only after CAD success.

Completed transaction IDs are idempotent. Reapplying one returns its stored
result without duplicating objects. Pending transactions are process-local and
expire after `topology.transaction_ttl_seconds`.

### Semantic relations

- `top:containsElement` — room-to-wall and host-wall-to-opening containment
- `top:isPartOf` — inverse room/host membership
- `top:adjacentTo` — shared room boundary adjacency
- `top:connectsTo` — room connectivity derived through doors
- `cad:hostWall` — scalar semantic wall identifier used by the CAD planner

The scalar `cad:hostWall` is intentionally separate from the JSON-LD relation
list, preventing scalar/list collisions during graph serialization.

### Sidecars and source of truth

The DWG and its XData remain the authoring source of truth. `export` and
successful `apply` write both files to the configured output directory:

```text
<drawing>.topology.jsonld
<drawing>.topology.ttl
```

Turtle is generated deterministically from the validated graph rather than
using TopologicPy's slower `TTLString` path.

### 7. Design Orchestration Engine (`design_engine/`)

The high-level service is deliberately separate from MCP registration and CAD
COM implementation:

```text
manage_design typed request
        ↓
DesignOrchestrator
├── AnalysisCache          revision-keyed pure graph cache
├── semantic index/walker  bounded affected-scope context
├── ExecutionPlan          deterministic local stages
├── topology planner       geometry, host, dependency validation
├── RetryPolicy            one retry for recognized CAD transients
├── FailureMemory          stops the same failure after two attempts
├── TransactionStore       preview/apply/cancel/rollback lifecycle
└── MetricsStore           local call/operation/byte/time proxies
        ↓
CADTopologyBridge → thread-local adapter → AutoCAD
```

The cache stores only immutable `DrawingSnapshot` data and serializable analysis
results. A COM proxy never enters it. Each CAD call still captures a cheap
fingerprintable snapshot so the server can prove whether the active revision is
unchanged; expensive topology analysis is skipped on a cache hit.

`get_context` builds a bidirectional semantic index from explicit JSON-LD
relationships and scalar host/parent IDs, then performs a depth- and
entity-bounded traversal. Truncated results are paged from process-local result
storage without reconnecting to CAD.

The execution plan orders deterministic dependencies, such as walls before
hosted doors/windows, and categorizes validation findings as `AUTO_FIXABLE`,
`NEEDS_LLM_DECISION`, or `FATAL`. Only the approved transaction is passed to the
bridge. The LLM is not placed between individual geometry operations.

Successful mutation invalidates earlier drawing revisions and performs a full
post-apply graph rebuild. Fine-grained graph surgery remains deferred until it
can preserve the same correctness guarantees.

---

## Design Patterns

| Pattern | Location | Purpose |
| :--- | :--- | :--- |
| **Mixin Composition** | AutoCADAdapter | Modular functionality |
| **Singleton + thread local** | AdapterRegistry, ConfigManager | Shared configuration without cross-thread COM proxies |
| **Context Manager** | com_session, SelectionSetManager | Resource cleanup |
| **Decorator** | @cad_tool, @com_safe | Cross-cutting concerns |
| **Abstract Base** | CADInterface | Contract definition |
| **Dataclass** | All configs | Type-safe configuration |

---

## Exception Hierarchy

```
TopoSpatialError (or MultiCADError)
├── CADConnectionError     (cad_type, reason)
├── CADOperationError      (operation, reason)
├── InvalidParameterError  (param, value, expected)
│   ├── CoordinateError
│   └── ColorError
├── LayerError             (layer_name, reason)
├── CADNotSupportedError   (cad_type, supported_cads)
└── ConfigError            (config_file, reason)
```

---

## Architectural Strengths

### 1. Clean Layer Separation

```
Core (abstractions) ← Adapters (implementation) ← Infrastructure ← Tools ← Server
```

- **0 circular dependencies**
- Each layer only knows the one below
- Easily swappable implementations

### 2. Typed Contracts

- Type hints on all functions and methods
- Pydantic models & dataclasses for configuration and payloads
- Type annotations throughout the public adapter/tool contracts

### 3. Robust Resource Management

```python
with com_session():
    with SelectionSetManager(doc, "TempSet") as ss:
        ss.SelectAll()
```

### 4. Performance Optimizations

| Optimization | Impact |
| :--- | :--- |
| **Thread-local connection reuse** | Avoids unnecessary reconnects without sharing COM proxies |
| **Batch Operations** | 60-70% fewer API calls |
| **High-level design transaction** | One MCP preview for all dependent semantic changes |
| **Revision-keyed topology cache** | Avoids repeat analysis on unchanged drawings |
| **Affected-scope traversal** | Keeps unrelated graph nodes out of model context |
| **Compact response levels** | Omits geometry/diff/debug data by default |
| **Pickfirst Selection Set** | Fast entity access |
| **Deferred Refresh** | `_skip_refresh=True` for batches |
| **Handle-to-Object Lookup** | O(1) direct entity access |
