# TopoSpatial-CAD MCP

**Spatial Topology Engine, Architectural Ontology & Autonomous CAD Intelligence via Model Context Protocol (MCP)**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-FastMCP%203.1%2B-green)](https://github.com/jlowin/fastmcp)
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

### Current implementation status

- **8 unified MCP tools** for session, drawing, layer, entity, block, file,
  export, and topology workflows.
- **Native AutoCAD Architecture authoring** for `AecDbWall`, `AecDbDoor`, and
  `AecDbWindow`, with runtime AEC API/style discovery.
- **Portable fallback geometry** for ordinary AutoCAD, ZWCAD, GstarCAD, and
  BricsCAD.
- **Explicit room/wall/door/window semantics** persisted in
  `TOPOSPATIAL_TOPOLOGY` XData and exported to JSON-LD and Turtle.
- **Preview/apply transactions** with a 10-minute default expiry, drawing
  revision checks, idempotent apply, one AutoCAD undo group, and verified
  rollback.
- **228 automated tests passing** on the current Windows development setup.

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

### 4. Deterministic Transaction Pipeline
Autonomous agents are never permitted to execute destructive geometry edits directly. All modifications follow a safe, deterministic transaction lifecycle:

```
Analyze DWG ──► Query/Reason ──► Preview & Diff ──► Apply ──► Export/Verify
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

### 7. Native AutoCAD Architecture Authoring

When AutoCAD Architecture is the active product, topology transactions can
create native `AecDbWall`, `AecDbDoor`, and `AecDbWindow` objects. Doors and
windows are attached with native opening-to-wall anchors, so ACA wall cleanup,
styles, schedules, and object editing remain available. The server discovers the
installed AEC automation version at runtime; it is not tied to a single ACA year.

Each create or update change accepts a `representation` policy:

- `auto` (default): use native AEC objects when ACA is available and the opening
  has a native wall host; otherwise use standard AutoCAD entities.
- `native_aec`: require native ACA authoring and fail preview if it is unavailable
  or the opening host is not a native wall.
- `standard`: always use portable lines, arcs, and polylines.

The selected representation is frozen into the preview transaction and persisted
in XData, preventing preview/apply drift. Inspect support and installed styles with
`manage_session` action `capabilities` before preparing a transaction.

---

### 8. DWG & JSON-LD as Source of Truth
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

TopoSpatial-CAD MCP provides **8 unified tools** covering dozens of CAD and
topology operations:

| Tool | Actions / Scope | Description |
| :--- | :--- | :--- |
| **`manage_topology`** | `analyze`, `query`, `preview`, `apply`, `export` | Explicit 2D room/wall/door/window semantics, native ACA authoring, relationship queries, and JSON-LD/Turtle export. |
| **`draw_entities`** | `line`, `circle`, `arc`, `rectangle`, `polyline`, `spline`, `text`, `dimension`, `leader`, `mleader`, `table` | High-speed parameterized geometric drawing with shorthand aliases such as `rect`. |
| **`manage_blocks`** | `list`, `info`, `create`, `insert`, `get_attrs`, `set_attrs` | Full block definition, insertion, and dynamic attribute tag read/write. |
| **`manage_layers`** | `list`, `info`, `create`, `delete`, `rename`, `turn_on`, `turn_off`, `set_color`, `is_on` | Layer state management and filtering; shorthand `on`/`off` aliases are supported. |
| **`manage_entities`** | `select`, `move`, `rotate`, `scale`, `set_color`, `set_layer`, `set_color_bylayer`, `copy`, `paste`, `delete` | Handle-based entity manipulation and property assignments. |
| **`manage_files`** | `new`, `save`, `close`, `list`, `switch` | Multi-drawing tab management and DWG/DXF/PDF save/export. |
| **`manage_session`** | `connect`, `disconnect`, `status`, `capabilities`, `check_running`, `list_supported`, `zoom_extents`, `screenshot`, `export_view`, `undo`, `redo`, `open_dashboard` | Thread-local connection handling, native ACA/style discovery, viewport control, capture, history, and diagnostics. |
| **`export_data`** | `json`, `excel` (all / selected entities) | Drawing data extraction with automated Excel multi-sheet reports. |

---

## Installation & Setup

### Prerequisites
- **Windows OS** (COM automation)
- **Python 3.10+**
- A running, COM-capable **AutoCAD, AutoCAD Architecture, ZWCAD, GstarCAD, or
  BricsCAD** installation
- **AutoCAD Architecture** is required only for native AEC objects; other
  products use standard entity fallback

### 1. Quick Install

```powershell
# Install uv package manager (if needed)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Clone and setup environment
git clone https://github.com/Nihalmannath/TopoSpatial-CAD-MCP.git
cd TopoSpatial-CAD-MCP

# Sync dependencies
uv sync --extra dev
uv run python -m pip install --upgrade pywin32
```

To enable the optional, pinned TopologicPy spatial topology engine:
```powershell
uv sync --extra dev --extra topology
```

This installs `topologicpy==0.9.65` and `topologic-core==8.0.4`. The server
forces the verified `topologic_core` backend before importing TopologicPy.

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
> Replace `C:\path\to\TopoSpatial-CAD-MCP` with your local repository path,
> point directly to `.venv\Scripts\python.exe`, then completely restart Claude
> Desktop. Open CAD before asking the client to connect.

### 3. First connection and native ACA check

In an MCP client, call `manage_session` with these operations:

```json
[
  {"action": "status"},
  {"action": "capabilities", "include_styles": true}
]
```

For AutoCAD Architecture, confirm that the result contains
`"native_aec": true` and the expected wall, door, and window styles. Before a
topology mutation, set the active drawing's `INSUNITS` to millimetres (`4`).

### 4. Recommended agent workflow

1. Run `manage_session` `status` and `capabilities`.
2. Run `manage_topology(action="analyze")` and keep its SHA-256 revision.
3. Submit a strict change document to `manage_topology(action="preview")`.
4. Review the returned diff, warnings, representation, and affected handles.
5. Apply the returned transaction ID exactly once; repeated apply is safe and
   returns the stored result.
6. Query or analyze again, save the DWG, and export JSON-LD/Turtle as needed.

For architectural objects, explicitly tell the agent: **use
`manage_topology`, not `draw_entities`**. The generic drawing tool intentionally
creates standard CAD geometry and does not invoke ACA wall/door/window objects.

See [Native ACA and topology workflow](docs/07-NATIVE-ACA-TOPOLOGY.md) for a
complete 5000 × 4000 mm room example with one door and two windows.

---

## Verification & Testing

```powershell
# Run the full test suite
uv run pytest -q

# Check the files changed in your branch
uv run ruff check <changed-files>

# Build the documentation site
uv run --extra docs mkdocs build --strict
```

The repository contains some pre-existing whole-tree lint debt; use targeted
Ruff checks for changed files until that baseline is cleaned up.

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
├── tests/                     # Unit, transaction, adapter, and integration tests
├── docs/                      # Complete architecture & setup documentation
└── mkdocs.yml                 # Documentation site configuration
```

## Documentation

- [Installation and MCP client setup](docs/01-SETUP.md)
- [System architecture](docs/02-ARCHITECTURE.md)
- [Native ACA and topology workflow](docs/07-NATIVE-ACA-TOPOLOGY.md)
- [Complete MCP tool reference](docs/05-REFERENCE.md)
- [Troubleshooting](docs/04-TROUBLESHOOTING.md)
- [Changelog](docs/03-CHANGELOG.md)

---

## Author & License

- **Author**: [Nihal Ahmed Mannath](https://github.com/Nihalmannath) (`nihalmannat@gmail.com`)
- **License**: [Apache License 2.0](LICENSE)
