"""
File mixin for AutoCAD adapter.

Handles file operations (save, open, close, new, switch, recycle).
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core import get_config

logger = logging.getLogger(__name__)

_DELETABLE_DRAWING_EXTENSIONS = frozenset({".dwg", ".dxf"})


class FileMixin:
    """Mixin for file operations."""

    if TYPE_CHECKING:
        document: Any

        def _get_document(self, operation: str = "operation") -> Any: ...
        def _get_application(self, operation: str = "operation") -> Any: ...
        def _validate_document(self) -> bool: ...
        def resolve_export_path(
            self, filename: str, folder_type: str = "drawings"
        ) -> str: ...

    def save_drawing(
        self, filepath: str = "", filename: str = "", format: str = "dwg"
    ) -> bool:
        """Save drawing to file.

        Args:
            filepath: Full path to save file (e.g., 'C:/drawings/myfile.dwg')
            filename: Just the filename (e.g., 'myfile.dwg'). If provided without
                     filepath, uses configured output directory
            format: File format (dwg, dxf, etc.). Default: dwg

        Returns:
            bool: True if successful, False otherwise

        Note:
            - If both filepath and filename provided, filepath takes precedence
            - If only filename provided, saved to config output directory
            - If neither provided, uses current document name
        """
        try:
            document = self._get_document("save_drawing")
            config = get_config()

            # SECURITY: Resolve output directory first (reference for validation)
            _output_dir = Path(config.output.directory).expanduser().resolve()

            # ========== Determine Filename ==========
            if filepath:
                save_filename = Path(filepath).name
            elif filename:
                save_filename = filename
            else:
                save_filename = None

            # If still no filename, use document name or generate one
            if not save_filename:
                if document.Name:
                    save_filename = document.Name
                else:
                    from datetime import datetime

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_filename = f"drawing_{timestamp}.{format}"

            # ========== Ensure Correct File Extension ==========
            # Only add extension if it's not already there
            if not save_filename.lower().endswith(f".{format}"):
                save_filename = f"{save_filename}.{format}"

            # ========== Resolve Final Path ==========
            if (
                filepath
                and Path(filepath).is_absolute()
                and getattr(config.output, "allow_arbitrary_paths", False)
            ):
                final_path = Path(filepath)
            else:
                # Use centralized export path resolution for standard saves
                final_path_str = self.resolve_export_path(save_filename, "drawings")
                final_path = Path(final_path_str)

            # Save the drawing
            document.SaveAs(str(final_path))
            logger.info(f"Saved drawing to {final_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save drawing: {e}")
            return False

    def open_drawing(self, filepath: str) -> bool:
        """Open a drawing file in the CAD application via COM.

        Args:
            filepath: Absolute path to the drawing file to open (for example,
                ``C:/drawings/plan.dwg``).

        Returns:
            True if the file was opened successfully, False otherwise.
        """
        try:
            application = self._get_application("open_drawing")
            self.document = application.Documents.Open(filepath)
            logger.info(f"Opened drawing from {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to open drawing: {e}")
            return False

    def new_drawing(self) -> bool:
        """Create a new blank drawing document in the CAD application via COM.

        Returns:
            True if the document was created successfully, False otherwise.
        """
        try:
            application = self._get_application("new_drawing")
            self.document = application.Documents.Add()
            self._refresh_document_reference()
            logger.info("Created new blank drawing")
            return True
        except Exception as e:
            logger.error(f"Failed to create new drawing: {e}")
            return False

    def _refresh_document_reference(self, auto_create: bool = True) -> bool:
        """Refresh internal document reference to ActiveDocument.

        This ensures self.document always points to the active document
        in the application. Useful after creating or switching documents.

        Args:
            auto_create: If True and no documents open, create a new one (default: True)

        Returns:
            True if successful, False otherwise
        """
        try:
            application = self._get_application("_refresh_document_reference")

            # Case 1: Documents are open, use the active one
            if application.Documents.Count > 0:
                self.document = application.ActiveDocument
                if self.document is not None:
                    logger.debug(f"Document reference refreshed: {self.document.Name}")
                return True

            # Case 2: No documents open
            if auto_create:
                logger.warning("No documents open. Creating a new blank document...")
                self.document = application.Documents.Add()
                if self.document is not None:
                    logger.info(f"Auto-created new document: {self.document.Name}")
                return True
            else:
                logger.warning("No documents open")
                return False

        except Exception as e:
            logger.error(f"Failed to refresh document reference: {e}")
            return False

    def get_open_drawings(self) -> list:
        """Get list of all open drawing filenames.

        Returns:
            List of drawing names (e.g., ["drawing1.dwg", "drawing2.dwg"])
        """
        try:
            application = self._get_application("get_open_drawings")
            drawings = []

            # Use direct iteration instead of Item indexing
            for doc in application.Documents:
                drawings.append(doc.Name)

            logger.info(f"Found {len(drawings)} open drawings: {drawings}")
            return drawings
        except Exception as e:
            logger.error(f"Failed to get open drawings: {e}")
            return []

    def switch_drawing(self, drawing_name: str) -> bool:
        """Switch to a different open drawing.

        Args:
            drawing_name: Name of the drawing to switch to (e.g., "drawing1.dwg")

        Returns:
            True if successful, False otherwise
        """
        try:
            application = self._get_application("switch_drawing")

            # Use direct iteration instead of Item indexing
            for doc in application.Documents:
                if doc.Name == drawing_name:
                    # Intenta activar a través del documento
                    doc.Activate()
                    # Intenta también forzarlo a nivel de aplicación (ZWCAD/AutoCAD)
                    try:
                        application.ActiveDocument = doc
                    except Exception:
                        pass

                    self.document = doc

                    # Pump Windows messages briefly to let CAD process the GUI switch
                    try:
                        import pythoncom

                        for _ in range(5):
                            pythoncom.PumpWaitingMessages()
                    except ImportError:
                        pass

                    logger.info(f"Switched to drawing: {drawing_name}")
                    return True

            logger.warning(f"Drawing not found: '{drawing_name}'")
            return False

        except Exception as e:
            logger.error(f"Failed to switch drawing: {e}")
            return False

    def close_drawing(self, save_changes: bool = False) -> bool:
        """Close the current drawing.

        Args:
            save_changes: Whether to save changes before closing (default: False)
                         True = save changes
                         False = discard changes without prompting

        Returns:
            True if successful, False otherwise
        """
        try:
            if not self._validate_document() or self.document is None:
                logger.warning("No document to close")
                return False

            document = self.document
            doc_name = document.Name

            # Close document using COM API
            document.Close(save_changes)

            # Try to update connection to remaining open document
            refresh_success = self._refresh_document_reference(auto_create=False)

            if refresh_success and self.document is not None:
                logger.info(
                    f"Closed drawing: {doc_name} (save_changes={save_changes}). "
                    f"Switched to: {self.document.Name}"
                )
            else:
                # No other documents open - attempt to create one to maintain connection
                logger.warning(
                    f"No other documents open after closing {doc_name}. "
                    "Attempting to create a new blank document..."
                )
                try:
                    application = self._get_application("close_drawing_reconnect")
                    self.document = application.Documents.Add()
                    logger.info(
                        f"Closed drawing: {doc_name} (save_changes={save_changes}). "
                        f"Created new document: {self.document.Name}"
                    )
                except Exception as e:
                    self.document = None
                    logger.info(
                        f"Closed drawing: {doc_name} (save_changes={save_changes}). "
                        "Could not create new document."
                    )
                    logger.debug(f"Auto-create error: {e}")

            return True

        except Exception as e:
            logger.error(f"Failed to close drawing: {e}")
            return False

    def delete_drawing_file(
        self, target: str, include_sidecars: bool = False
    ) -> dict[str, Any]:
        """Move one closed MCP-output drawing to the Windows Recycle Bin.

        Deletion is deliberately narrower than saving: the exact target must be
        a ``.dwg`` or ``.dxf`` inside the configured output root, even when
        ``allow_arbitrary_paths`` is enabled. Open drawings are refused so an
        agent cannot implicitly discard an unsaved CAD document.

        Args:
            target: Exact filename or path. A filename resolves in ``drawings``.
            include_sidecars: Also recycle matching topology JSON-LD/Turtle.

        Returns:
            Structured result containing the recycled absolute paths.

        Raises:
            ValueError: If the target is unsafe, unsupported, or currently open.
            FileNotFoundError: If the exact drawing file does not exist.
            RuntimeError: If Windows rejects or aborts the recycle operation.
        """
        drawing_path, output_root = self._resolve_drawing_delete_path(target)
        if not drawing_path.exists() or not drawing_path.is_file():
            raise FileNotFoundError(f"Drawing file was not found: {drawing_path}")

        open_name = self._matching_open_drawing(drawing_path)
        if open_name:
            raise ValueError(
                f"Refusing to delete open drawing '{open_name}'. Close it explicitly "
                "with manage_files before deleting the file."
            )

        recycle_paths = [drawing_path]
        if include_sidecars:
            stem = drawing_path.stem
            for suffix in (".topology.jsonld", ".topology.ttl"):
                sidecar = (output_root / f"{stem}{suffix}").resolve()
                sidecar.relative_to(output_root)
                if sidecar.exists() and sidecar.is_file():
                    recycle_paths.append(sidecar)

        self._move_to_recycle_bin(recycle_paths)
        logger.info(
            "Moved drawing output to Recycle Bin: %s",
            ", ".join(str(path) for path in recycle_paths),
        )
        return {
            "success": True,
            "detail": "Drawing moved to Windows Recycle Bin",
            "path": str(drawing_path),
            "recycled": [str(path) for path in recycle_paths],
            "sidecars_included": include_sidecars,
            "recoverable": True,
        }

    @staticmethod
    def _resolve_drawing_delete_path(target: str) -> tuple[Path, Path]:
        """Resolve and validate one exact drawing target without creating paths."""
        raw_target = str(target or "").strip()
        if not raw_target:
            raise ValueError("delete requires an exact drawing filename or path")
        if any(character in raw_target for character in "*?[]"):
            raise ValueError("Wildcards and glob patterns are not allowed for delete")

        config = get_config()
        output_root = Path(config.output.directory).expanduser().resolve()
        requested = Path(raw_target).expanduser()
        if requested.is_absolute():
            resolved = requested.resolve()
        elif requested.parent == Path("."):
            resolved = (output_root / "drawings" / requested.name).resolve()
        else:
            resolved = (output_root / requested).resolve()

        try:
            resolved.relative_to(output_root)
        except ValueError as exc:
            raise ValueError(
                "Drawing deletion is restricted to the configured output directory: "
                f"{output_root}"
            ) from exc
        if resolved.suffix.casefold() not in _DELETABLE_DRAWING_EXTENSIONS:
            allowed = ", ".join(sorted(_DELETABLE_DRAWING_EXTENSIONS))
            raise ValueError(f"Only CAD drawing files can be deleted: {allowed}")
        return resolved, output_root

    def _matching_open_drawing(self, candidate: Path) -> str | None:
        """Return the matching open document name, if any."""
        application = self._get_application("delete_drawing_file")
        candidate_key = str(candidate).casefold()
        candidate_name = candidate.name.casefold()
        for document in application.Documents:
            name = str(getattr(document, "Name", ""))
            if name.casefold() == candidate_name:
                return name
            try:
                full_name = str(getattr(document, "FullName", ""))
                if (
                    full_name
                    and str(Path(full_name).resolve()).casefold() == candidate_key
                ):
                    return name or full_name
            except Exception:
                continue
        return None

    @staticmethod
    def _move_to_recycle_bin(paths: list[Path]) -> None:
        """Recycle exact local paths through the Windows Shell API."""
        if not paths:
            raise ValueError("No files were provided for recycling")
        from win32com.shell import shell, shellcon

        sources = "\0".join(str(path) for path in paths) + "\0\0"
        flags = (
            shellcon.FOF_ALLOWUNDO
            | shellcon.FOF_NOCONFIRMATION
            | shellcon.FOF_SILENT
            | shellcon.FOF_NOERRORUI
        )
        result, aborted = shell.SHFileOperation(
            (0, shellcon.FO_DELETE, sources, None, flags, None, None)
        )
        if result != 0:
            raise RuntimeError(f"Windows recycle operation failed with code {result}")
        if aborted:
            raise RuntimeError("Windows recycle operation was aborted")
