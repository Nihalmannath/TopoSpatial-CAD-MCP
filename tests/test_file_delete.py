"""Safe drawing-file deletion tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from adapters.mixins.file_mixin import FileMixin
from mcp_tools.tools.files import _delete, _save


class _FileAdapter(FileMixin):
    def __init__(self, documents=()):
        self._application = SimpleNamespace(Documents=list(documents))

    def _get_application(self, operation: str = "operation"):
        return self._application


def _config(root: Path, allow_arbitrary_paths: bool = False):
    return SimpleNamespace(
        output=SimpleNamespace(
            directory=str(root),
            allow_arbitrary_paths=allow_arbitrary_paths,
        )
    )


def test_delete_requires_confirmation_before_adapter_call():
    """The tool does not touch the adapter without explicit confirmation."""
    result = _delete({"target": "old.dwg", "confirm": False})
    assert result["success"] is False
    assert result["requires_confirmation"] is True


def test_delete_resolves_filename_in_drawings_and_recycles_sidecars(tmp_path):
    """A confirmed closed drawing and requested sidecars are recycled together."""
    drawings = tmp_path / "drawings"
    drawings.mkdir()
    drawing = drawings / "example.dwg"
    jsonld = tmp_path / "example.topology.jsonld"
    turtle = tmp_path / "example.topology.ttl"
    drawing.write_bytes(b"dwg")
    jsonld.write_text("{}", encoding="utf-8")
    turtle.write_text("@prefix top: <urn:test:> .", encoding="utf-8")
    adapter = _FileAdapter()
    recycle = MagicMock()

    with patch("adapters.mixins.file_mixin.get_config", return_value=_config(tmp_path)):
        with patch.object(adapter, "_move_to_recycle_bin", recycle):
            result = adapter.delete_drawing_file(
                "example.dwg", include_sidecars=True
            )

    assert result["success"] is True
    assert result["recoverable"] is True
    recycle.assert_called_once_with([drawing, jsonld, turtle])


def test_delete_refuses_open_drawing(tmp_path):
    """Deletion never closes or discards an open CAD document implicitly."""
    drawings = tmp_path / "drawings"
    drawings.mkdir()
    drawing = drawings / "open_plan.dwg"
    drawing.write_bytes(b"dwg")
    document = SimpleNamespace(Name="open_plan.dwg", FullName=str(drawing))
    adapter = _FileAdapter([document])

    with patch("adapters.mixins.file_mixin.get_config", return_value=_config(tmp_path)):
        with patch.object(adapter, "_move_to_recycle_bin") as recycle:
            with pytest.raises(ValueError, match="Refusing to delete open drawing"):
                adapter.delete_drawing_file("open_plan.dwg")

    recycle.assert_not_called()


@pytest.mark.parametrize("target", ["*.dwg", "plan?.dwg", "folder/[ab].dwg"])
def test_delete_refuses_wildcards(tmp_path, target):
    """Bulk/glob deletion is not part of the file tool contract."""
    with patch("adapters.mixins.file_mixin.get_config", return_value=_config(tmp_path)):
        with pytest.raises(ValueError, match="Wildcards"):
            _FileAdapter._resolve_drawing_delete_path(target)


def test_delete_refuses_non_cad_extension(tmp_path):
    """The operation cannot be repurposed as a general filesystem delete."""
    with patch("adapters.mixins.file_mixin.get_config", return_value=_config(tmp_path)):
        with pytest.raises(ValueError, match="Only CAD drawing files"):
            _FileAdapter._resolve_drawing_delete_path("notes.txt")


def test_delete_stays_in_output_root_even_when_arbitrary_saves_are_enabled(
    tmp_path,
):
    """The broader save opt-in does not broaden destructive deletion scope."""
    outside = tmp_path.parent / "outside.dwg"
    config = _config(tmp_path, allow_arbitrary_paths=True)
    with patch("adapters.mixins.file_mixin.get_config", return_value=config):
        with pytest.raises(ValueError, match="restricted"):
            _FileAdapter._resolve_drawing_delete_path(str(outside))


def test_shell_recycle_failure_is_reported(tmp_path):
    """A Windows Shell error never falls back to permanent deletion."""
    drawing = tmp_path / "example.dwg"
    with patch(
        "win32com.shell.shell.SHFileOperation", return_value=(5, False)
    ):
        with pytest.raises(RuntimeError, match="code 5"):
            _FileAdapter._move_to_recycle_bin([drawing])


def test_confirmed_tool_call_delegates_exact_flags():
    """The MCP handler forwards only the exact target and sidecar choice."""
    adapter = MagicMock()
    adapter.delete_drawing_file.return_value = {
        "success": True,
        "recycled": ["C:/exports/drawings/example.dwg"],
    }
    with patch("mcp_tools.tools.files.get_current_adapter", return_value=adapter):
        result = _delete(
            {
                "target": "example.dwg",
                "confirm": True,
                "include_sidecars": True,
            }
        )
    assert result["success"] is True
    adapter.delete_drawing_file.assert_called_once_with(
        "example.dwg", include_sidecars=True
    )


def test_save_result_reports_the_actual_drawings_subdirectory(tmp_path):
    """The tool response matches the path used by FileMixin."""
    adapter = MagicMock()
    adapter.save_drawing.return_value = True
    config = _config(tmp_path)
    with patch("mcp_tools.tools.files.get_current_adapter", return_value=adapter):
        with patch("mcp_tools.tools.files.get_config", return_value=config):
            result = _save({"filename": "example.dwg"})

    assert result["path"] == str(tmp_path / "drawings" / "example.dwg")
