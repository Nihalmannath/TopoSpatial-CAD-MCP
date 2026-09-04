# Autodesk AutoCAD Setup for TopoSpatial-CAD

TopoSpatial-CAD MCP connects directly to your local AutoCAD installation via Windows COM automation (`ActiveX / COM`).

## Supported AutoCAD Versions
- **AutoCAD 2027**
- **AutoCAD 2026**
- **AutoCAD 2025**
- **AutoCAD 2024**
- *(Also compatible with ZWCAD 2024-2026, GstarCAD, and BricsCAD)*

## Operating System Requirements
- Microsoft Windows 10 or 11 (64-bit)
- Python 3.10+ (64-bit)
- Claude Desktop for Windows

## AutoCAD Configuration Checklist
1. **Standard Elevation**: Run AutoCAD at standard user elevation (do not run as Administrator unless Claude Desktop is also run as Administrator, because Windows COM forbids cross-integrity IPC).
2. **Open Drawing**: Keep at least one `.dwg` file open in AutoCAD so the active document bridge can interact with the ModelSpace.
3. **Double-click Diagnostics**: You can run `autodesk/check_cad_health.bat` or `python -m server.main --doctor` anytime to verify COM communication.
