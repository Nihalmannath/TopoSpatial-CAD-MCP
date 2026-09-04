"""
TopoSpatial-CAD MCP Entry Point
Configured as the bundle entrypoint for Claude Desktop and CLI execution.

Supports:
  python -m server.main           (Launches MCP server via stdio/HTTP)
  python -m server.main --doctor  (Runs pre-flight CAD and environment diagnostics)
  python -m server.main --check   (Alias for --doctor)
"""

import sys
import os
from pathlib import Path

# Ensure bundle root and src directory are in Python path
current_dir = Path(__file__).resolve().parent
bundle_root = current_dir.parent
src_dir = bundle_root / "src"

for path in [str(bundle_root), str(src_dir)]:
    if path not in sys.path:
        sys.path.insert(0, path)

# Setup UTF-8 I/O encoding immediately for Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--doctor", "--check", "-c", "doctor"):
        from server.doctor import print_doctor_report
        print_doctor_report()
        sys.exit(0)

    # Launch main TopoSpatial-CAD MCP server
    try:
        from server import mcp  # fastmcp server instance in src/server.py
        use_stdio = not sys.stdin.isatty() or os.environ.get("MCP_TRANSPORT") == "stdio"

        if use_stdio:
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="stdio")
    except ImportError as e:
        # Fallback to direct src.server execution
        try:
            import src.server as srv
            srv.mcp.run(transport="stdio")
        except Exception as exc:
            print(f"[TopoSpatial-CAD] Fatal startup error: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
