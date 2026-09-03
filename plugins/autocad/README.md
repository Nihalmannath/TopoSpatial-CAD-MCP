# AutoCAD TopoSpatial Studio Plugin

Version 0.3.0 embeds the local `/editor` architectural program and topology
studio in a dockable AutoCAD palette. Live mode is read-only; Sandbox edits an
isolated, revision-pinned semantic draft and requires Preview then Apply.

## Registered AutoCAD Commands

| Command | Purpose |
| :--- | :--- |
| `TOPOSTUDIO` | Opens the dockable TopoSpatial Studio palette with 2D Floor Plan, Circulation Graph, and Space Schedule. |
| `TOPOSTUDIORELOAD` | Reloads the studio WebView2 frame without restarting AutoCAD. |
| `TOPOSTUDIOEVENTS` | The only command that toggles live CAD events. They remain off at initialize, open, reload, and document activation. |
| `TOPOSTATUS` | Reports plugin/backend/frontend version, AutoCAD product, drawing, event state, revision, draft state, and MCP inbox count. |
| `TOPOZOOM` | Prompts for an entity handle and smoothly centers the AutoCAD viewport on that object. |
| `TOPOPROGRAM` | Prints a summary of the current space programming and room areas to the AutoCAD command line. |

## Compatibility & Targets

AutoCAD 2024 and 2025 require separate builds:

- `2024/TopoSpatial.AutoCAD.2024.csproj`: .NET Framework 4.8.
- `2025/TopoSpatial.AutoCAD.2025.csproj`: .NET 8 for Windows.

## Build and Package

Build and package with a matching Autodesk installation:

```powershell
.\plugins\autocad\build.ps1 -Version 2024
```

The script produces `plugins/autocad/dist/TopoSpatial.bundle` and a distributable ZIP. Copy the bundle to `%APPDATA%\Autodesk\ApplicationPlugins`, restart AutoCAD, start the MCP service, and run `TOPOSTUDIO`. For a quick test, use `NETLOAD` on the DLL in `Contents/Windows/<version>/`.

The WebView bridge accepts only typed version-1 `focus_entity`, `select_entity`,
and `request_status` messages from `http://127.0.0.1:8888`. Every message must
name the pinned drawing. A mismatch returns structured JSON and never activates
another document.

## Automated Installation

To install the packaged bundle with a recoverable backup of any previous build:

```powershell
.\plugins\autocad\install.ps1
```
