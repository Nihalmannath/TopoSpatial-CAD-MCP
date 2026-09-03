"""
Connection mixin for AutoCAD adapter.

Handles connection, disconnection, and validation methods.
"""

import logging
import sys
from typing import TYPE_CHECKING, Any

if sys.platform == "win32":
    import win32com.client
    import pythoncom
    import pywintypes
else:
    from unittest.mock import MagicMock
    win32com = MagicMock()
    pythoncom = MagicMock()
    pywintypes = MagicMock()

from core import CADConnectionError, CADBusyError
from adapters.com_worker import (
    get_com_worker,
    is_com_busy_error,
    is_com_not_running_error,
)

if TYPE_CHECKING:
    from core.config import CADConfig

logger = logging.getLogger(__name__)


class ConnectionMixin:
    """Mixin for connection management."""

    if TYPE_CHECKING:
        # Tell type checker this mixin is used with CADAdapterProtocol
        cad_type: str
        config: "CADConfig"
        application: Any
        document: Any

        def _wait_for(
            self, condition: Any, timeout: float = 20.0, interval: float = 0.1
        ) -> bool: ...

    def _get_active_com_object(self) -> Any:
        """Attach to the running CAD instance without requiring ProgID lookup.

        Packaged desktop clients can launch the MCP child with a registry view
        in which ``CLSIDFromProgID`` fails even though the CAD object is present
        in the Running Object Table.  An explicitly configured CLSID bypasses
        that lookup while preserving the normal ProgID path everywhere else.
        """
        if win32com is None:
            raise CADConnectionError(self.cad_type, "COM support is only available on Windows")

        try:
            return win32com.client.GetActiveObject(self.config.prog_id)
        except Exception as prog_id_error:
            if is_com_busy_error(prog_id_error):
                raise CADBusyError(
                    self.cad_type,
                    f"GetActiveObject({self.config.prog_id}) rejected: application is busy or in a modal dialog: {prog_id_error}",
                ) from prog_id_error

            com_clsid = getattr(self.config, "com_clsid", None)
            if not com_clsid:
                if is_com_not_running_error(prog_id_error):
                    raise CADConnectionError(self.cad_type, "CAD application is not running") from prog_id_error
                raise prog_id_error

            try:
                clsid = pywintypes.IID(com_clsid)
                unknown = pythoncom.GetActiveObject(clsid)
                dispatch = unknown.QueryInterface(pythoncom.IID_IDispatch)
                application = win32com.client.Dispatch(dispatch)
                logger.info(
                    "%s instance found (active via configured CLSID)",
                    self.cad_type,
                )
                return application
            except Exception as clsid_error:
                if is_com_busy_error(clsid_error):
                    raise CADBusyError(
                        self.cad_type,
                        f"GetActiveObject({com_clsid}) rejected: application is busy or in a modal dialog: {clsid_error}",
                    ) from clsid_error
                if is_com_not_running_error(clsid_error):
                    raise CADConnectionError(self.cad_type, "CAD application is not running") from clsid_error
                raise clsid_error from prog_id_error

    def connect(self, only_if_running: bool = True, allow_launch: bool = False) -> bool:
        """Connect to the CAD application via COM, initializing COM for this thread.

        By default, attaches ONLY to an already-running instance (only_if_running=True, allow_launch=False).
        AutoCAD is launched via Dispatch() ONLY when explicitly requested via session/start (allow_launch=True).

        Args:
            only_if_running: When True, do not launch a new CAD instance if none is running.
            allow_launch: When False, never launch a new CAD instance via Dispatch().

        Returns:
            True if the connection was established successfully.

        Raises:
            CADBusyError: If AutoCAD is busy, running a command, or in a modal dialog.
            CADConnectionError: If connection fails or CAD is not running.
        """
        # Fast-fail if circuit breaker is open
        get_com_worker().circuit_breaker.check(self.cad_type)

        try:
            logger.info(
                f"Connecting to {self.cad_type} (only_if_running={only_if_running}, allow_launch={allow_launch})..."
            )

            # Initialize COM for this thread
            if sys.platform == "win32" and pythoncom is not None:
                try:
                    pythoncom.CoInitialize()
                except Exception as e:
                    logger.debug(f"CoInitialize: {e}")

            # Try to get existing running instance
            try:
                self.application = self._get_active_com_object()
                logger.info(
                    f"{self.cad_type} instance found (active via GetActiveObject)"
                )
            except Exception as e:
                if is_com_busy_error(e):
                    get_com_worker().circuit_breaker.record_busy(self.cad_type, str(e))
                    logger.warning(f"{self.cad_type} is busy: {e}")
                    if isinstance(e, CADBusyError):
                        raise
                    raise CADBusyError(self.cad_type, str(e)) from e

                if only_if_running or not allow_launch:
                    logger.debug(
                        f"{self.cad_type} not running (only_if_running={only_if_running}, allow_launch={allow_launch}). Skipping launch."
                    )
                    return False

                # Launch only when allow_launch=True and only_if_running=False (session/start)
                logger.info(
                    f"Explicit user start requested: launching new {self.cad_type} instance..."
                )
                try:
                    dispatch_id = getattr(self.config, "com_clsid", None) or self.config.prog_id
                    self.application = win32com.client.Dispatch(dispatch_id)
                except Exception as com_err:
                    error_code = getattr(com_err, "args", [None])[0]
                    if error_code == -2147221005:
                        error_msg = (
                            f"Invalid ProgID '{self.config.prog_id}'. "
                            f"Either {self.cad_type.upper()} is not installed or the ProgID is incorrect. "
                            f"Check config.json and ensure the application is installed."
                        )
                    else:
                        error_msg = str(com_err)
                    logger.error(
                        f"Failed to create {self.cad_type} instance: {error_msg}"
                    )
                    raise CADConnectionError(self.cad_type, error_msg) from com_err

                if self.application is not None:
                    # Try to make application visible (not all CAD types support this)
                    try:
                        self.application.Visible = True
                    except (pywintypes.com_error, AttributeError) as e:
                        logger.debug(
                            f"{self.cad_type} doesn't support Visible property or it's read-only: {e}"
                        )
                self._wait_for(
                    lambda: self.application is not None,
                    timeout=self.config.startup_wait_time,
                )
                logger.info(
                    f"New {self.cad_type} instance started "
                    f"(waited {self.config.startup_wait_time}s)"
                )

            # Get active document or create new
            if self.application is not None:
                try:
                    try:
                        has_docs = int(self.application.Documents.Count) > 0
                    except (TypeError, ValueError):
                        has_docs = bool(self.application.Documents.Count)

                    if has_docs:
                        self.document = self.application.ActiveDocument
                        logger.info("Using existing active document")
                    else:
                        self.document = self.application.Documents.Add()
                        logger.info("Created new document")
                except Exception as doc_err:
                    if is_com_busy_error(doc_err):
                        get_com_worker().circuit_breaker.record_busy(self.cad_type, str(doc_err))
                        raise CADBusyError(self.cad_type, f"AutoCAD busy while accessing document: {doc_err}") from doc_err
                    raise CADConnectionError(self.cad_type, f"Failed accessing document: {doc_err}") from doc_err

            # Validate connection
            if not self._validate_document():
                raise CADConnectionError(self.cad_type, "Document validation failed")

            get_com_worker().circuit_breaker.record_success()
            logger.info(f"Connected to {self.cad_type}")

            return True

        except (CADBusyError, CADConnectionError):
            raise
        except Exception as e:
            if is_com_busy_error(e):
                get_com_worker().circuit_breaker.record_busy(self.cad_type, str(e))
                raise CADBusyError(self.cad_type, str(e)) from e
            error_msg = f"COM error: {str(e)}"
            logger.error(f"Failed to connect to {self.cad_type}: {error_msg}")
            raise CADConnectionError(self.cad_type, error_msg) from e

    def disconnect(self) -> bool:
        """Disconnect from CAD application with COM cleanup."""
        try:
            if self.application:
                self.application = None
                self.document = None
            if sys.platform == "win32" and pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            logger.info(f"Disconnected from {self.cad_type}")
            return True
        except Exception as e:
            logger.error(f"Error disconnecting: {e}")
            return False

    def __enter__(self):
        """Enter context: connect to CAD application.

        Allows using the adapter as a context manager:
            with AutoCADAdapter("autocad") as adapter:
                adapter.draw_line((0,0), (10,10))
                # Auto-disconnect on exit

        Returns:
            Self (the adapter instance)

        Raises:
            CADConnectionError: If connection fails
        """
        if not self.connect(only_if_running=True, allow_launch=False):
            raise CADConnectionError(
                self.cad_type, "Connection failed during context manager initialization (CAD not running)"
            )
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Exit context: disconnect from CAD application.

        Args:
            _exc_type: Exception type if raised
            _exc_val: Exception value if raised
            _exc_tb: Exception traceback if raised
        """
        self.disconnect()

    def is_connected(self) -> bool:
        """Check if connected to CAD application."""
        try:
            return (
                self.application is not None
                and self.document is not None
                and self._validate_document()
            )
        except Exception:
            return False

    def _validate_document(self) -> bool:
        """Validate that document is accessible."""
        try:
            if self.document is None:
                return False
            _ = self.document.Name
            return True
        except Exception as e:
            if is_com_busy_error(e):
                raise CADBusyError(self.cad_type, f"AutoCAD busy during document validation: {e}") from e
            return False

    def check_document_change(self) -> bool:
        """
        Check if the active document in the CAD application has changed.
        If it has, update self.document to the new active document.

        Returns:
            bool: True if the document changed, False otherwise.
        """
        try:
            if not self.application:
                return False

            # If no documents are open, there's nothing to check
            if self.application.Documents.Count == 0:
                if self.document is not None:
                    self.document = None
                    return True
                return False

            active_doc = self.application.ActiveDocument

            # If we didn't have a document before, but now we do
            if self.document is None:
                self.document = active_doc
                return True

            # Compare names
            if self.document.Name != active_doc.Name:
                self.document = active_doc
                logger.info(f"Active document changed to: {active_doc.Name}")
                return True

            return False
        except Exception as e:
            # Silently catch COM errors during polling
            logger.debug(f"Error checking document change: {e}")
            return False
