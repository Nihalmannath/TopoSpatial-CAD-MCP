# TopoSpatial-CAD MCP

**Spatial Topology Engine, Architectural Ontology & Autonomous CAD Intelligence via Model Context Protocol (MCP)**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-FastMCP%202.0-green)](https://github.com/jlowin/fastmcp)
[![Author](https://img.shields.io/badge/Author-Nihal%20Ahmed%20Mannath-orange)](https://github.com/Nihalmannath)

---

## Executive Overview

Standard CAD automation interfaces treat drawings as collections of disconnected, "dumb" coordinate geometry: lines, polylines, arcs, and blocks. When a Large Language Model (LLM) interacts with raw CAD data, it is forced to guess relationships from coordinate soup (`Polyline handle 274`, `Line handle 281`, `Block at x=5230, y=3120`).

**TopoSpatial-CAD MCP transforms AutoCAD, ZWCAD, GstarCAD, and BricsCAD into a semantically structured, topology-aware architectural model for autonomous AI agents and assistants (Claude Desktop, Cursor, VS Code).**

Instead of an LLM perceiving:
> *"Four polylines + one block + two lines"*

TopoSpatial-CAD MCP constructs a rich spatial knowledge graph:
> - **Bedroom-01** (20 m² clear interior usable area) contains **Door-03** and **Window-02**.
> - **Bedroom-01** is **topologically adjacent to Corridor-01**.
> - **Door-03** connects **Bedroom-01** directly to **Corridor-01**.

```
┌────────────────────────────────────────────────────────┐
│                   AI ARCHITECTURE AGENT                │
│             (Semantic & Spatial Reasoning)             │
└───────────────────────────┬────────────────────────────┘
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
  ┌───────────────────┐           ┌───────────────────┐
  │  Spatial Topology │           │ Architectural     │
  │  (TopologicPy)    │           │ Ontology (JSON-LD)│
  └─────────┬─────────┘           └─────────┬─────────┘
            └───────────────┬───────────────┘
                            ▼
               ┌─────────────────────────┐
               │ TopoSpatial Engine Core │
               │ (Transaction Validation)│
               └────────────┬────────────┘
                            ▼
               ┌─────────────────────────┐
               │   AutoCAD / ZWCAD COM   │
               │ (DWG + XData Persistence│
               └─────────────────────────┘
```

---

## Core Architectural Innovations

```
"TopoSpatial-CAD MCP gives AI hands.
 TopologicPy gives it spatial understanding.
 The ontology gives it architectural vocabulary.
 JSON-LD gives it structured memory.
 Preview / Apply gives it safety."
```

### 1. Topology vs. Ontology: The Dual-Layer Brain
- **Topology** answers: *How are objects geometrically related?* (Containment, adjacency, connectivity, boundary closure, opening host relationships).
- **Ontology** answers: *What are those objects?* (`semantic_id = ROOM-BED-004`, `class = Bedroom`, `label = Guest Bedroom`, `host = WALL-019`).

This distinction enables high-level architectural reasoning:
- *"Which bedrooms don't have direct access to a corridor?"*
- *"Which habitable rooms lack exterior window access?"*
- *"Enlarge Bedroom 2 by 600 mm without reducing the corridor width below 1200 mm."*
- *"Find all rooms directly connected to the main entrance lobby."*

---

### 2. Strict "Do Not Infer Semantic Classes" Rule
To eliminate LLM hallucinations, **unannotated enclosed geometric boundaries are treated strictly as candidate rooms** (`candidate_room_23`, `area: 13.5m²`) until explicitly designated by the architect or an automated design verification workflow.

```
AutoCAD Drawing               Candidate Detection            Explicit Classification
┌──────────────┐               ┌─────────────────┐             ┌─────────────────────┐
│              │     ───►      │candidate_room_23│    ───►     │ semantic_id: BED-01 │
│              │               │area: 13.5 m²    │             │ class: Bedroom      │
└──────────────┘               └─────────────────┘             └─────────────────────┘
```

---

### 3. Persistent DWG XData Storage (`TOPOSPATIAL_TOPOLOGY`)
Intelligence does not disappear when an LLM chat session ends. Semantic identities, ontology classes, group assignments, and schema versions are embedded directly into CAD entities as native **Extended Entity Data (XData)**.

When you close AutoCAD, restart your computer, and reopen the project tomorrow, **Wall-12 is still Wall-12, Door-04 is still Door-04, and Bedroom-02 is still Bedroom-02**.

---

### 4. Deterministic 5-Stage Transaction Pipeline (`analyze → query → preview → apply`)
Autonomous agents are never permitted to execute destructive geometry edits directly. All modifications follow a safe, deterministic transaction lifecycle:

```
LLM Reasoning ──► Analyze DWG ──► Generate Preview & Diff ──► Validate Rules ──► Apply Transaction
```

**Example Transaction Preview:**
```text
TRANSACTION: TX-72A91
Action: Move Kitchen East Wall +600 mm
Affected Entities: Kitchen-01, Wall-18, Door-07, LivingRoom-01
Dimensional Impact:
  - Kitchen clear width: 3600 mm → 4200 mm
  - Living Room clear width: 5100 mm → 4500 mm
Warning:
  - Door-07 host wall displaced by 600 mm (requires repositioning)
Status: Awaiting Architect Approval
```

---

### 5. SHA-256 Drawing Fingerprint & Revision Safety
Every analysis creates a SHA-256 geometric drawing fingerprint (`revision = AF84C2...`). If an architect manually adjusts a wall in AutoCAD while the AI is reasoning, TopoSpatial-CAD MCP detects the revision mismatch and rejects outdated transactions to prevent geometric corruption.

---

### 6. Clear Interior Dimension Contract
In architectural practice, room dimensions always refer to **clear interior usable space**. TopoSpatial-CAD MCP enforces this contract at the core API level: requesting a 5000 × 4000 mm bedroom automatically calculates wall thickness offsets (e.g. outer footprint 5400 × 4400 mm with 200 mm walls).

---

### 7. DWG & JSON-LD as Source of Truth
TopologicPy operates strictly as an on-demand **computational layer**, ensuring that your underlying project data remains portable and vendor-neutral in standard DWG XData and JSON-LD graphs.

```json
{
  "@context": {"top": "http://w3id.org/topologicpy#", "cad": "urn:topospatial:cad#"},
  "@id": "room:bedroom-01",
  "@type": "arch:Bedroom",
  "label": "Master Bedroom",
  "clearWidth": 5000,
  "clearDepth": 4000,
  "containsElement": ["door:01", "window:03"],
  "adjacentTo": ["room:corridor-01"]
}
```

---

## Core Capabilities & Tools

TopoSpatial-CAD MCP provides **8 unified tools** dispatching 56+ CAD commands:

| Tool | Actions / Scope | Description |
| :--- | :--- | :--- |
| **`manage_topology`** | `analyze`, `query`, `preview`, `apply`, `export_ontology` | 2D room/boundary analysis, door/window detection, topological graph reasoning, and JSON-LD / Turtle ontology generation. |
| **`draw_entities`** | `line`, `circle`, `arc`, `rect`, `pline`, `spline`, `text`, `table` | High-speed parameterized geometric drawing with shorthand parsing support. |
| **`manage_blocks`** | `list`, `info`, `create`, `insert`, `get_attrs`, `set_attrs` | Full block definition, insertion, and dynamic attribute tag read/write. |
| **`manage_layers`** | `list`, `create`, `delete`, `rename`, `on`, `off`, `lock`, `unlock`, `color` | Layer state management and filtering. |
| **`manage_entities`** | `select`, `move`, `rotate`, `scale`, `copy`, `paste`, `delete`, `color` | Handle-based entity manipulation and property assignments. |
| **`manage_files`** | `new`, `open`, `save`, `close`, `list`, `switch` | Multi-drawing tab management and export to DWG / DXF / PDF. |
| **`manage_session`** | `connect`, `status`, `zoom_extents`, `undo`, `redo`, `dashboard` | Connection pooling, viewport navigation, and session diagnostics. |
| **`export_data`** | `json`, `excel` (all / selected entities) | Drawing data extraction with automated Excel multi-sheet reports. |

---

## Installation & Setup

### Prerequisites
- **Windows OS** (COM automation)
- **Python 3.10+**
- Supported CAD installed: **AutoCAD (2018+)**, **ZWCAD (2020+)**, **GstarCAD (2020+)**, or **BricsCAD (21+)**

### 1. Quick Install

```powershell
# Install uv package manager (if needed)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Clone and setup environment
git clone https://github.com/Nihalmannath/TopoSpatial-CAD-MCP.git
cd TopoSpatial-CAD-MCP

# Sync dependencies
uv sync --dev
uv run python -m pip install --upgrade pywin32
```

To enable the optional TopologicPy spatial topology engine:
```powershell
uv sync --extra dev --extra topology
```

### 2. Claude Desktop Integration

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "topospatial": {
      "command": "C:\\path\\to\\TopoSpatial-CAD-MCP\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\TopoSpatial-CAD-MCP\\src\\server.py"]
    }
  }
}
```

> [!IMPORTANT]
> Replace `C:\path\to\TopoSpatial-CAD-MCP` with your local repository path, ensuring you point directly to `.venv\Scripts\python.exe`.

---

## Verification & Testing

```powershell
# Run the test suite (181 tests)
uv run pytest -m "not slow and not integration" tests/unit

# Type checking
uv run mypy --config-file mypy.ini src/server.py
```

---

## Project Structure

```
TopoSpatial-CAD-MCP/
├── src/
│   ├── server.py              # FastMCP entry point
│   ├── __version__.py         # Version & author metadata
│   ├── config.json            # Runtime configuration
│   ├── core/                  # Interfaces, ConfigManager, Models
│   ├── adapters/              # Mixin-based COM adapter layer
│   ├── mcp_tools/             # Shorthand dispatchers & tool registrations
│   │   └── tools/             # 8 unified MCP tool modules
│   ├── topology_engine/       # Spatial topology, XData schemas, transactions
│   ├── ui/                    # UI resources and inspector templates
│   └── web/                   # Real-time CAD status dashboard
├── tests/                     # 181 unit & integration tests
├── docs/                      # Complete architecture & setup documentation
└── mkdocs.yml                 # Documentation site configuration
```

---

## Author & License

- **Author**: [Nihal Ahmed Mannath](https://github.com/Nihalmannath) (`nihalmannat@gmail.com`)
- **License**: [Apache License 2.0](LICENSE)
