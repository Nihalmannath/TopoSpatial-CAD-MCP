# 01 - Development Setup

## Prerequisites

- Python 3.10+
- Windows OS with COM support
- CAD application (AutoCAD, ZWCAD, GstarCAD, or BricsCAD)
- AutoCAD Architecture only when native `AecDbWall`, `AecDbDoor`, and
  `AecDbWindow` objects are required

## Installation
 
### 1. Install `uv`

`uv` is the recommended Python package manager for this project. To install it on Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

For other methods or platforms, see the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

### 2. Clone and Setup

```powershell
# Clone
git clone https://github.com/Nihalmannath/TopoSpatial-CAD-MCP.git
cd TopoSpatial-CAD-MCP

# Install development dependencies (creates .venv automatically)
uv sync --extra dev
uv run python -m pip install --upgrade pywin32

# Verify
uv run pytest -q
uv run python src/server.py
```

For 2D architectural topology and ontology support, install the pinned optional
TopologicPy backend as well:

```powershell
uv sync --extra dev --extra topology
```

This installs the tested pair `topologicpy==0.9.65` and
`topologic-core==8.0.4`. The server sets
`TOPOLOGICPY_CORE_BACKEND=topologic_core` before importing TopologicPy. Restart
the MCP client after adding the extra.

Verify both packages without starting CAD:

```powershell
uv run --extra topology python -c "import topologicpy, topologic_core; print('Topology OK')"
```

**Note**: If you get an execution policy error:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

## Claude Desktop Integration

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "topospatial": {
      "command": "C:\\path\\to\\TopoSpatial-CAD-MCP\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\TopoSpatial-CAD-MCP\\src\\server.py"],
      "env": {
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

**Important**:

1. Use absolute paths and the `.venv\Scripts\python.exe` created by `uv sync`,
   not the system `py` launcher.
2. Completely quit and reopen Claude Desktop after changing its configuration.
3. Start CAD in the same Windows user session and at the same privilege level as
   Claude Desktop. Open a drawing before the first CAD request.
4. Check `manage_session` with `status` and `capabilities` before asking for
   native architectural objects.

The first diagnostic call should pass this native array to `operations` (a
JSON-encoded string remains accepted for older clients):

```json
[
  {"action": "status"},
  {"action": "capabilities", "include_styles": true}
]
```

For topology mutations, set `INSUNITS` to millimetres (`4`) in the active DWG.
Prefer `manage_design` for room/wall/door/window creation and modification.
`manage_topology` remains available for direct graph/export work;
`draw_entities` creates generic CAD geometry.

## Project Structure

```
TopoSpatial-CAD-MCP/
├── src/
│   ├── server.py              # FastMCP entry point
│   ├── __version__.py         # Package version
│   ├── config.json            # Runtime configuration
│   ├── core/                  # Abstract interfaces
│   │   ├── cad_interface.py   # CADInterface ABC
│   │   ├── config.py          # ConfigManager singleton
│   │   ├── exceptions.py      # Exception hierarchy
│   │   └── models.py          # Data models and schemas
│   ├── adapters/              # CAD implementations
│   │   ├── autocad_adapter.py # Composite class (102 lines)
│   │   ├── adapter_manager.py # AdapterRegistry
│   │   └── mixins/            # 12 mixins, including native ACA support
│   ├── mcp_tools/             # Server infrastructure
│   │   ├── constants.py       # COLOR_MAP, etc.
│   │   ├── helpers.py         # Utilities
│   │   ├── decorators.py      # @cad_tool
│   │   ├── shorthand.py       # Command parsing logic
│   │   ├── validator.py       # Spec validation and correction
│   │   └── tools/             # 9 unified tools
│   ├── design_engine/         # Orchestration, cache, retries, metrics
│   ├── topology_engine/       # 2D semantics, transactions, sidecars
│   ├── ui/                    # UI resources and templates
│   └── web/                   # Web dashboard API and static files
├── tests/                     # pytest suite
├── docs/                      # Documentation
└── logs/                      # Auto-generated logs
```

## Key Commands

```powershell
uv run pytest -q                            # Run all 286 tests
uv run ruff check <changed-files>           # Lint files changed in your branch
uv run ruff format <changed-files>          # Format files changed in your branch
uv run --extra docs mkdocs build --strict   # Validate documentation
npx -y @modelcontextprotocol/inspector uv run python src/server.py  # MCP Inspector
```

The current repository has pre-existing whole-tree Ruff findings. Do not apply
an unreviewed global `ruff --fix`; keep new and modified files clean while that
baseline is reduced separately.

## Git Workflow

### Repository
**URL**: https://github.com/Nihalmannath/TopoSpatial-CAD-MCP

### Branch Naming
- `feature/<description>` - New features
- `fix/<description>` - Bug fixes
- `refactor/<description>` - Refactoring
- `docs/<description>` - Documentation

### Commit Convention
Use the following format: `<type>(<scope>): <subject>`
- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation
- **refactor**: Code refactoring
- **test**: Tests
- **chore**: Build, dependencies

**Example**: `git commit -m "feat(blocks): add insert_block tool"`

## Development Tips

1. **Type hints everywhere** - enables IDE autocomplete
2. **Absolute imports** - `from core import X`, not `from ..core`
3. **Log operations** - use `logger.info()` and `logger.debug()`
4. **Test first** - add tests before committing (`uv run pytest -q`)
5. **Format & lint changed files** - avoid unrelated whole-tree rewrites

## Next Steps

- [02-ARCHITECTURE.md](02-ARCHITECTURE.md) - Understand the design and how to extend it
- [07-NATIVE-ACA-TOPOLOGY.md](07-NATIVE-ACA-TOPOLOGY.md) - Native ACA and safe topology workflow
- [04-TROUBLESHOOTING.md](04-TROUBLESHOOTING.md) - Debugging guide
