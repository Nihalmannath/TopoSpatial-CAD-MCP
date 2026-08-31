# 02 - System Architecture

## Overview

```
┌────────────────────────────────────────────────────────┐
│  TopoSpatial-CAD MCP Server (server.py)                │
│  8 unified tools + CAD commands + Spatial Topology     │
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
│  ├── ... (11 mixins total)         │
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

Entry point that registers **8 unified MCP tools** via FastMCP. Seven tools dispatch established CAD commands; `manage_topology` provides the architectural spatial topology and ontology workflow.

### 2. Tools (`mcp_tools/tools/`)

8 modules, each providing one unified tool or workflow:

| Module | Tool | Actions | Responsibility |
| :--- | :--- | :--- | :--- |
| `topology.py` | `manage_topology` | 5 | Analyze, query, preview, apply, export ontology |
| `session.py` | `manage_session` | 11 | Connection, view, history, dashboard |
| `drawing.py` | `draw_entities` | 10 | Unified parameterized entity creation |
| `blocks.py` | `manage_blocks` | 6 | Block management & attribute tags |
| `layers.py` | `manage_layers` | 9 | Layer management & queries |
| `files.py` | `manage_files` | 5 | File operations & format conversion |
| `entities.py` | `manage_entities` | 10 | Select, move, rotate, scale, color |
| `export.py` | `export_data` | 4 | Data extraction & formatted Excel |

### 3. Adapter Layer (`adapters/`)

Uses a mixin-based composition architecture:

```python
class AutoCADAdapter(
    CADInterface,
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
):
    """Composite adapter combining all mixin capabilities."""
```

Each mixin handles a single domain:
- **ConnectionMixin**: Connects to running CAD, manages document lifecycle
- **DrawingMixin**: Creates geometry (lines, circles, arcs, text, tables)
- **LayerMixin**: Layer operations (create, toggle, rename, lock, color)
- **FileMixin**: Save, export (DWG, DXF, PDF), switch drawings
- **ViewMixin**: Zoom, fit view, undo/redo
- **SelectionMixin**: Entity selection by type, layer, handle, color
- **EntityMixin**: Query properties, extract bounding boxes
- **ManipulationMixin**: Move, rotate, scale, copy, delete, change color/layer
- **BlockMixin**: List, query, insert, create blocks, manage attributes
- **ExportMixin**: Extract structured data, export to formatted Excel
- **UtilityMixin**: Color conversion, coordinate validation, helpers

### 4. Adapter Manager (`adapters/adapter_manager.py`)

- **`AdapterRegistry`**: Thread-safe singleton holding initialized adapters
- **`get_adapter(cad_type)`**: Returns adapter for requested or active CAD
- **Auto-detection**: Probes running CAD processes and connects automatically

### 5. Core Abstractions (`core/`)

- **`CADInterface`**: Abstract base class defining CAD contracts
- **`ConfigManager`**: Singleton with cascading config search
- **`TopoSpatialError`**: Comprehensive exception hierarchy

### 6. Spatial Topology Engine (`topology_engine/`)

The topology layer receives immutable, plain-coordinate snapshots from the CAD bridge. COM entities never leave the calling thread. It builds explicit semantic nodes from `TOPOSPATIAL_TOPOLOGY` XData, reports untagged enclosed areas as candidates, derives spatial relationships, validates strict change documents, and stores expiring preview transactions. Apply runs inside one CAD undo group; JSON-LD and deterministic Turtle sidecars are updated only after CAD modifications succeed.

---

## Design Patterns

| Pattern | Location | Purpose |
| :--- | :--- | :--- |
| **Mixin Composition** | AutoCADAdapter | Modular functionality |
| **Singleton** | AdapterRegistry, ConfigManager | Shared state |
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

### 2. 100% Type Safety

- Type hints on all functions and methods
- Pydantic models & dataclasses for configuration and payloads
- Clean type-checking with `mypy`

### 3. Robust Resource Management

```python
with com_session():
    with SelectionSetManager(doc, "TempSet") as ss:
        ss.SelectAll()
```

### 4. Performance Optimizations

| Optimization | Impact |
| :--- | :--- |
| **Connection Pooling** | Avoids 5-20s reconnections |
| **Batch Operations** | 60-70% fewer API calls |
| **Pickfirst Selection Set** | Fast entity access |
| **Deferred Refresh** | `_skip_refresh=True` for batches |
| **Handle-to-Object Lookup** | O(1) direct entity access |
