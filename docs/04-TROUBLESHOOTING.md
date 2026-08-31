# Troubleshooting

## Quick Reference

| Symptom | Likely cause | First action |
|---|---|---|
| CAD is open but MCP says disconnected | Wrong Python/repo, stale MCP process, privilege mismatch, or busy CAD UI | Run `status` and `capabilities`, then inspect the server log |
| `Call was rejected by callee` | AutoCAD is executing a command or showing a dialog | Activate AutoCAD, press Esc several times, and close dialogs |
| `manage_topology` is unavailable | Client has not restarted or is using another MCP configuration | Install the topology extra and completely restart the client |
| `native_aec` is false | Ordinary AutoCAD is active or AEC automation is unavailable | Start AutoCAD Architecture and rerun `capabilities` |
| Preview says drawing changed | SHA-256 revision is stale | Analyze again and create a new preview |
| Preview requires millimetres | `INSUNITS` is not `4` | Set the active drawing units to millimetres |
| Unknown ACA style | Requested wall/door/window style is not installed | Use a style returned by `capabilities` |
| Drawing looks correct but has no semantic nodes | Agent used `draw_entities` | Use `manage_topology` or explicitly annotate existing geometry |
| Screenshot shows another app or is cropped | Old server lacks HWND/PrintWindow/DPI handling | Restart the MCP client on the updated code |

## Claude Desktop Cannot See CAD

Claude Desktop launches a stdio MCP server itself; a second manually started
server is not required. Verify `%APPDATA%\Claude\claude_desktop_config.json` uses
absolute paths to this repository's virtual environment:

```json
{
  "mcpServers": {
    "topospatial": {
      "command": "C:\\path\\to\\TopoSpatial-CAD-MCP\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\TopoSpatial-CAD-MCP\\src\\server.py"],
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

Then check, in order:

1. Completely quit Claude Desktop, including its tray/background process.
2. Start AutoCAD or AutoCAD Architecture and open a drawing.
3. Ensure CAD and Claude run as the same Windows user and privilege level. Do
   not run one as Administrator and the other normally.
4. Reopen Claude and call `manage_session` with this `operations` value:

   ```json
   [
     {"action": "check_running"},
     {"action": "status"},
     {"action": "capabilities", "include_styles": true}
   ]
   ```

5. Confirm Claude used the `topospatial` integration and the response names the
   active product. If it used another CAD MCP, disable the duplicate or tell the
   agent explicitly which integration/tool to call.

## Direct COM Diagnostic

From the repository directory:

```powershell
uv run python -c "import win32com.client; a=win32com.client.GetActiveObject('AutoCAD.Application'); print(a.Name, a.Version, a.ActiveDocument.Name)"
```

If this succeeds but the MCP fails, the client is normally using the wrong
virtual environment, an older checkout, or a stale process. If it returns
`Call was rejected by callee`:

1. Bring AutoCAD to the foreground.
2. Press Esc several times to cancel an unfinished command.
3. Close any hidden Save As, warning, license, or style dialog.
4. Retry the diagnostic.

Save open work before restarting CAD. Restarting is a last resort, not the first
fix.

## Pywin32 or Registration Failure

```powershell
uv sync --extra dev
uv run python -m pip install --upgrade pywin32
uv run python -c "import pythoncom, win32com.client; print('pywin32 OK')"
```

If `GetActiveObject` still cannot find a running product, verify that CAD is
fully started (not on a first-run installer/login screen) and that its COM
ProgID matches `src/config.json`.

## Topology Dependency Failure

Install and verify the pinned backend:

```powershell
uv sync --extra dev --extra topology
uv run --extra topology python -c "import topologicpy, topologic_core; print('Topology OK')"
```

The server forces `TOPOLOGICPY_CORE_BACKEND=topologic_core`. Restart the MCP
client after installation. If imports still fail, confirm Claude's configured
`command` points to the same `.venv\Scripts\python.exe` used above.

## Native ACA Objects Are Not Created

First inspect capabilities:

```json
[
  {"action": "capabilities", "include_styles": true}
]
```

- `native_aec: false` means the active product does not expose ACA automation.
- `native_aec: true` with a style error means native support exists, but style
  enumeration failed. Resolve that before using explicit `native_aec`.
- A native door/window requires a native `AecDbWall` semantic host.
- `auto` safely falls back to standard geometry; `native_aec` fails preview
  instead of silently falling back.

Make sure the agent used `manage_topology`. `draw_entities` deliberately creates
standard lines, arcs, polylines, and blocks even inside AutoCAD Architecture.

## Preview or Apply Failure

### `base_revision does not match`

The active drawing changed after analysis. Analyze again and rebuild the change
document with the new revision.

### Transaction expired or disappeared

Preview transactions expire after 600 seconds by default and live in the MCP
server process. A server/client restart invalidates pending IDs. Analyze and
preview again.

### Missing host wall or opening beyond wall

Create/annotate the host wall before its door/window in the same change document.
`offset + width` must not exceed host wall length.

### Post-apply validation failed

The bridge closes the AutoCAD undo mark, performs one grouped undo, and waits for
the original revision. Sidecars are not replaced. Inspect the log, analyze the
drawing, and preview again rather than reusing the failed transaction.

## Drawing, Layer, or Capture Problems

### Drawing not visible

Call `manage_session` with:

```json
[
  {"action": "zoom_extents"},
  {"action": "screenshot"}
]
```

The screenshot path uses the live application HWND and `PrintWindow`, so another
foreground app does not replace the CAD image. High-DPI window bounds are
handled before capture.

### Geometry exists but is hidden

Use `manage_layers` `info`, then turn on the relevant layer. Topology-managed
layers are `AI-ROOMS`, `AI-WALLS`, `AI-DOORS`, and `AI-WINDOWS`. `AI-ROOMS` is
non-plot by design.

### Output is not where expected

The default configured root is:

```text
%USERPROFILE%\Documents\TopoSpatial Exports
```

DWGs saved by filename are normally placed in its `drawings` subdirectory.
Topology JSON-LD and Turtle sidecars are placed directly in the configured root.
Screenshots are placed in `images`.

## Configuration and Logs

After editing `src/config.json`, validate it and restart the MCP client:

```powershell
uv run python -m json.tool src/config.json
```

Set `"logging_level": "DEBUG"` for more detail. The main log is:

```text
logs\topospatial_mcp.log
```

The dashboard defaults to:

```text
http://127.0.0.1:8888
```

## Development Verification

```powershell
uv run pytest -q
uv run ruff check <changed-files>
uv run --extra docs mkdocs build --strict
npx -y @modelcontextprotocol/inspector uv run python src/server.py
```

The current verified suite contains 228 tests. The repository also has
pre-existing whole-tree Ruff findings, so lint changed files rather than applying
an unreviewed global autofix.

