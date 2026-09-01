"""
Unified file management tool.

Combines drawing file workflows in one tool using a simple shorthand format
for ~85% token reduction.

SHORTHAND FORMAT (one per line):
    save|path_or_filename|format  → save|/path/to/file.dwg
    save|filename                 → save|backup.dwg
    new                           → new
    close|save_changes            → close|true
    list                          → list
    switch|drawing_name           → switch|floor_plan.dwg
    delete|target|confirm|sidecars → delete|old_plan.dwg|true|false
"""

import json
import logging
import os
from typing import Optional, Dict, Any, Callable, List, Tuple, Union


from core import get_config
from mcp_tools.decorators import cad_tool, get_current_adapter
from mcp_tools.shorthand import parse_file_ops_input

logger = logging.getLogger(__name__)


# ========== Action Handlers ==========


def _save(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Save the current drawing to a file.

    Args:
        spec: Operation spec with keys: filepath (str, optional), filename (str, optional),
            format (str, optional): 'dwg' (default), 'dxf', or 'pdf'.

    Returns:
        Dict with keys: success (bool), detail (str), path (str, if saved to a path).
    """
    filepath = spec.get("filepath", "")
    filename = spec.get("filename", "")
    fmt = spec.get("format", "dwg")

    success = get_current_adapter().save_drawing(
        filepath=filepath, filename=filename, format=fmt
    )

    if success:
        config = get_config()
        output_dir = os.path.abspath(os.path.expanduser(config.output.directory))
        requested = filepath or filename
        if (
            filepath
            and os.path.isabs(filepath)
            and getattr(config.output, "allow_arbitrary_paths", False)
        ):
            saved_path = os.path.abspath(filepath)
        elif requested:
            saved_path = os.path.join(
                output_dir, "drawings", os.path.basename(requested)
            )
        else:
            saved_path = None

        result = {"success": True, "detail": "Drawing saved"}
        if saved_path:
            result["path"] = saved_path
        return result

    return {"success": False, "detail": "Failed to save drawing"}


def _new(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new drawing.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    success = get_current_adapter().new_drawing()
    return {
        "success": success,
        "detail": "New drawing created" if success else "Failed to create drawing",
    }


def _close(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Close the current drawing.

    Args:
        spec: Operation spec with keys: save_changes (bool, optional, default False).

    Returns:
        Dict with keys: success (bool), detail (str).
    """
    save_changes = spec.get("save_changes", False)
    adapter = get_current_adapter()
    success = adapter.close_drawing(save_changes=save_changes)

    if success:
        action = "saved and closed" if save_changes else "closed without saving"
        open_drawings = adapter.get_open_drawings()
        detail = f"Drawing {action}"
        if open_drawings:
            detail += f". Switched to: {open_drawings[0]}"
        else:
            detail += ". No other drawings open"
        return {"success": True, "detail": detail}

    return {"success": False, "detail": "Failed to close drawing"}


def _list(spec: Dict[str, Any]) -> Dict[str, Any]:
    """List all open drawings.

    Args:
        spec: Operation spec (no required keys).

    Returns:
        Dict with keys: success (bool), count (int), drawings (list), detail (str, if none open).
    """
    drawings = get_current_adapter().get_open_drawings()
    if not drawings:
        return {
            "success": True,
            "count": 0,
            "drawings": [],
            "detail": "No drawings open",
        }
    return {"success": True, "count": len(drawings), "drawings": drawings}


def _switch(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Switch the active drawing.

    Args:
        spec: Operation spec with keys: drawing_name (str).

    Returns:
        Dict with keys: success (bool), detail (str), available (list, if not found).
    """
    drawing_name = spec["drawing_name"]
    adapter = get_current_adapter()
    success = adapter.switch_drawing(drawing_name)

    if success:
        return {"success": True, "detail": f"Switched to: {drawing_name}"}

    available = adapter.get_open_drawings()
    return {
        "success": False,
        "detail": f"Drawing '{drawing_name}' not found",
        "available": available if available else [],
    }


def _delete(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Recycle one exact, closed CAD drawing after explicit confirmation."""
    target = str(
        spec.get("target") or spec.get("filepath") or spec.get("filename") or ""
    ).strip()
    if not target:
        return {
            "success": False,
            "error": "delete requires an exact target filename or path",
        }
    if spec.get("confirm") is not True:
        return {
            "success": False,
            "requires_confirmation": True,
            "target": target,
            "error": (
                "Deletion was not performed. Repeat with confirm=true after "
                "verifying the exact closed drawing."
            ),
        }
    return get_current_adapter().delete_drawing_file(
        target,
        include_sidecars=bool(spec.get("include_sidecars", False)),
    )


# Dispatch table: action -> (handler, required_fields)
FILE_DISPATCH: Dict[str, Tuple[Callable, List[str]]] = {
    "save": (_save, []),
    "new": (_new, []),
    "close": (_close, []),
    "list": (_list, []),
    "switch": (_switch, ["drawing_name"]),
    "delete": (_delete, []),
}


def _validate_required_fields(
    spec: Dict[str, Any], required: List[str], action: str
) -> Optional[str]:
    """Validate that required fields are present in spec.

    Args:
        spec: Operation spec dict to validate.
        required: List of field names that must be present.
        action: Action name used in the error message.

    Returns:
        Error message string if any fields are missing, otherwise None.
    """
    missing = [f for f in required if f not in spec]
    if missing:
        return f"'{action}' requires fields: {', '.join(missing)}"
    return None


# ========== Tool Registration ==========


def register_file_tools(mcp):
    """Register unified file management tool with FastMCP."""

    @cad_tool(mcp, "manage_files")
    def manage_files(
        operations: Union[str, Dict[str, Any], List[Dict[str, Any]]],
    ) -> str:
        """
        Manage drawing files with one or more operations in a single call.

        Args:
            operations: Operations in SHORTHAND format (one per line):

                save|path_or_filename|format  → save|/path/to/file.dwg
                save|filename                 → save|backup.dwg
                new                           → new
                close|save_changes            → close|true
                list                          → list
                switch|drawing_name           → switch|floor_plan.dwg
                delete|target|confirm|sidecars → delete|old_plan.dwg|true|false

                "format" = "dwg" (default), "dxf", "pdf"
                "save_changes" = true/false (default: false)
                "confirm" must be true for delete
                "sidecars" = true/false (default: false)

                Delete accepts one exact .dwg/.dxf inside the configured output
                directory, refuses open drawings and wildcards, and moves files
                to the Windows Recycle Bin. Close a drawing explicitly first.

                Example:
                    save|backup.dwg
                    new
                    switch|floor_plan.dwg
                    delete|old_plan.dwg|true|true

                JSON format also supported for backwards compatibility.



        Returns:
            JSON result with per-operation status
        """
        try:
            ops_data = parse_file_ops_input(operations)
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid input: {str(e)}",
                    "total": 0,
                    "succeeded": 0,
                    "results": [],
                },
                indent=2,
            )

        results = []

        for i, spec in enumerate(ops_data):
            action = spec.get("action")

            if not action:
                results.append(
                    {
                        "index": i,
                        "success": False,
                        "error": "Missing 'action' field. Supported: "
                        + ", ".join(FILE_DISPATCH.keys()),
                    }
                )
                continue

            action_lower = action.lower()
            dispatch_entry = FILE_DISPATCH.get(action_lower)

            if not dispatch_entry:
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": f"Unknown action '{action}'. Supported: "
                        + ", ".join(FILE_DISPATCH.keys()),
                    }
                )
                continue

            handler, required_fields = dispatch_entry

            field_error = _validate_required_fields(spec, required_fields, action_lower)
            if field_error:
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": field_error,
                    }
                )
                continue

            try:
                result = handler(spec)
                results.append({"index": i, "action": action_lower, **result})
            except Exception as e:
                logger.error(f"Error in file op {i} ({action_lower}): {e}")
                results.append(
                    {
                        "index": i,
                        "action": action_lower,
                        "success": False,
                        "error": str(e),
                    }
                )

        return json.dumps(
            {
                "total": len(ops_data),
                "succeeded": sum(1 for r in results if r.get("success")),
                "results": results,
            },
            indent=2,
        )
