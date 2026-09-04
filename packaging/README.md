# TopoSpatial CAD for Claude Desktop (.mcpb)

**Spatial Topology & Semantic Architectural CAD Intelligence for Autodesk AutoCAD**

TopoSpatial CAD empowers Claude Desktop to design, analyze, inspect, and modify AutoCAD architectural drawings directly through natural language and topological reasoning.

---

## Requirements

- **Operating System**: Windows 10 or 11 (64-bit)
- **Host Application**: [Claude Desktop](https://claude.ai/download) (latest release)
- **CAD Application**: Autodesk AutoCAD (2024, 2025, 2026, or 2027)
- **Python Runtime**: Python 3.10 or newer (64-bit)

---

## 1-Click Installation

1. **Download** `TopoSpatial-CAD-MCP.mcpb` from the [GitHub Releases](https://github.com/Nihalmannath/TopoSpatial-CAD-MCP/releases).
2. **Double-click** the `.mcpb` file (or drag it into Claude Desktop).
3. In Claude Desktop, click **Install**.
4. **Start AutoCAD** and open any drawing (`.dwg`).
5. In Claude Desktop, start chatting:
   > *"Inspect the active AutoCAD drawing and summarize all room boundaries."*
   > *"Generate a 2BHK floor plan layout adhering to vastu and standard dimensions."*

---

## Pre-flight Health Doctor

If you want to verify your AutoCAD and Python environment before starting:
```bash
python -m server.main --doctor
```
Or double-click `autodesk/check_cad_health.bat`.

---

## Capabilities

- **Architectural Synthesis**: Synthesizes clean room layouts, shared walls, and door/window placements.
- **Topological Computing**: Non-manifold cell analysis, adjacency graphs, and dual graph routing.
- **Safe Modifications**: Local execution planning, transaction preview/apply, and undo rollbacks.
- **Native AutoCAD Automation**: Interacts with ModelSpace, layers, blocks, and dimensions.

---

## License

TopoSpatial-CAD MCP is licensed under the [Apache License 2.0](LICENSE).
Author: [Nihal Mannath](https://github.com/Nihalmannath)
