"""
View mixin for AutoCAD adapter.

Handles view operations (zoom, refresh, undo, redo).
"""

import base64
import ctypes
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Dict

import win32con
import win32gui
import win32ui
from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)


class ViewMixin:
    """Mixin for view operations."""

    if TYPE_CHECKING:
        # Tell type checker this mixin is used with CADAdapterProtocol
        from typing import Any

        # Attributes from AutoCADAdapter
        cad_type: str

        def _get_application(self, operation: str = "operation") -> Any: ...
        def _get_document(self, operation: str = "operation") -> Any: ...
        def _simulate_autocad_click(self) -> bool: ...
        def _validate_connection(self) -> None: ...
        def resolve_export_path(
            self, filename: str, folder_type: str = "drawings"
        ) -> str: ...

    def _sanitize_command_input(self, user_input: str) -> str:
        """Sanitize input for SendCommand to prevent command injection.

        Restricts input to safe characters that are common in file paths and CAD
        commands.
        Non-matching characters are removed.

        Args:
            user_input: The user-provided input to sanitize

        Returns:
            str: The sanitized input safe for SendCommand
        """
        safe_pattern = re.compile(r"^[a-zA-Z0-9\s\\/._\-:()]+$")
        if not safe_pattern.match(user_input):
            logger.warning(f"Input sanitized due to unsafe characters: {user_input}")
            # Remove all characters that don't match safe pattern
            sanitized = re.sub(r"[^a-zA-Z0-9\s\\/._\-:()]", "", user_input)
            logger.debug(f"Sanitized to: {sanitized}")
            return sanitized
        return user_input

    def _find_cad_window(self) -> int:
        """Find CAD application window with strict matching.

        Uses both window title and class name matching to avoid VBA and other windows.
        Returns the handle of the main CAD window.

        Returns:
            int: Window handle (HWND) of the CAD application, 0 if not found

        Raises:
            Exception: If CAD window cannot be found
        """
        from mcp_tools.constants import (
            AUTOCAD_WINDOW_CLASSES,
            CAD_WINDOW_SEARCH_TERMS,
        )

        # Prefer the HWND exposed by the live COM application.  Product-specific
        # shells such as AutoCAD Architecture use titles like ``AutoCAD
        # Architecture 2024`` and versioned MFC classes (for example
        # ``AfxMDIFrame140u``), so title/class heuristics alone can miss the
        # correct main window even while the COM connection is healthy.
        try:
            application = self._get_application("find_cad_window")
            application_hwnd = int(getattr(application, "HWND", 0) or 0)
            if application_hwnd and win32gui.IsWindow(application_hwnd):
                logger.debug(
                    "Using CAD application HWND exposed by COM: %s",
                    application_hwnd,
                )
                return application_hwnd
        except Exception as exc:
            logger.debug("CAD application HWND lookup unavailable: %s", exc)

        search_term = CAD_WINDOW_SEARCH_TERMS.get(self.cad_type, "")
        hwnd = 0

        def enum_windows_callback(h, result):
            nonlocal hwnd
            if hwnd or not win32gui.IsWindowVisible(h):
                return

            title = win32gui.GetWindowText(h)
            class_name = win32gui.GetClassName(h)

            # Matching: title contains search term AND class is a CAD window class
            title_match = search_term.lower() in title.lower()
            if self.cad_type == "autocad":
                title_match = title_match or "autocad" in title.lower()
            class_match = any(p in class_name for p in AUTOCAD_WINDOW_CLASSES)
            if self.cad_type == "autocad":
                class_match = class_match or class_name.startswith("AfxMDIFrame")

            # Exclude VBA editor and other non-main windows
            if title_match and class_match and "VBA" not in title:
                hwnd = h
                logger.debug(
                    f"Found CAD window: title='{title}', class='{class_name}', hwnd={h}"
                )

        win32gui.EnumWindows(enum_windows_callback, None)

        if not hwnd:
            raise Exception(f"Could not find window for {self.cad_type}")

        return hwnd

    def get_screenshot(self) -> Dict[str, str]:
        """
        Capture a screenshot of the CAD application window.

        Returns:
            dict: Dictionary with 'path' and 'data' (base64)

        Raises:
            Exception: If screenshot fails
        """
        try:
            self._validate_connection()

            # Find the CAD window using strict matching
            hwnd = self._find_cad_window()

            # Python processes without a manifest are DPI-virtualized by Windows.
            # At 200% scaling, GetWindowRect would otherwise report half-size
            # bounds and PrintWindow would capture only the upper-left quadrant.
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception as exc:
                logger.debug("Could not enable DPI-aware screenshot bounds: %s", exc)

            # Get window bounds
            rect = win32gui.GetWindowRect(hwnd)
            x, y, w, h = rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
            logger.debug(f"Capturing screenshot for HWND {hwnd} at {x},{y} {w}x{h}")

            # PrintWindow renders the requested CAD window even when another app
            # covers it.  This is important for MCP hosts such as Claude Desktop
            # and Codex, which commonly remain foreground while requesting the
            # capture.  Fall back to a screen grab for CAD variants that do not
            # implement PrintWindow rendering.
            try:
                image = self._capture_window(hwnd, w, h)
            except Exception as exc:
                logger.warning("PrintWindow capture failed; using screen grab: %s", exc)
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                try:
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.2)
                except Exception as focus_exc:
                    logger.warning("Could not bring window to front: %s", focus_exc)
                image = ImageGrab.grab(
                    bbox=(x, y, x + w, y + h), all_screens=True
                )

            # Prepare filename and resolve path using centralized utility
            filename = f"cad_screenshot_{os.getpid()}.png"
            filepath = self.resolve_export_path(filename, "images")

            image.save(filepath, "PNG")

            # Convert to base64
            with open(filepath, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

            logger.info(f"Screenshot saved to {filepath}")

            return {"path": filepath, "data": encoded_string}

        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            raise Exception(f"Failed to capture screenshot: {e}")

    @staticmethod
    def _capture_window(hwnd: int, width: int, height: int) -> Image.Image:
        """Render one HWND without relying on foreground-window focus."""
        if width <= 0 or height <= 0:
            raise ValueError("CAD window has invalid dimensions")

        window_dc_handle = win32gui.GetWindowDC(hwnd)
        if not window_dc_handle:
            raise RuntimeError("Could not acquire the CAD window device context")
        source_dc = win32ui.CreateDCFromHandle(window_dc_handle)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            # PW_RENDERFULLCONTENT (2) asks DWM-backed windows for their complete
            # client surface instead of copying whatever happens to cover them.
            rendered = ctypes.windll.user32.PrintWindow(
                hwnd, memory_dc.GetSafeHdc(), 2
            )
            if not rendered:
                raise RuntimeError("PrintWindow did not render the CAD window")
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB",
                (info["bmWidth"], info["bmHeight"]),
                bits,
                "raw",
                "BGRX",
                0,
                1,
            ).copy()
        finally:
            if bitmap.GetHandle():
                win32gui.DeleteObject(bitmap.GetHandle())
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, window_dc_handle)

    def export_view(self) -> Dict[str, str]:
        """Export current view using internal PNGOUT command.

        This method uses ZWCAD's built-in PNGOUT command to export the drawing
        view to a PNG file. Unlike get_screenshot(), this method:
        - Works even if the window is minimized or obscured
        - Captures only the drawing content (no UI chrome)
        - Uses the CAD application's internal rendering

        Returns:
            Dictionary with 'path' and 'data' (base64 encoded image)

        Raises:
            Exception: If export fails
        """
        try:
            self._validate_connection()
            document = self._get_document("export_view")

            # Prepare filename and resolve path using centralized utility
            filename = f"cad_export_{os.getpid()}.png"
            filepath = self.resolve_export_path(filename, "images")

            # Ensure path uses backslashes for CAD command and is absolute
            filepath_cad = filepath.replace("/", "\\")

            logger.debug(f"Exporting view to {filepath_cad}")

            # Find the CAD window for focusing (with error handling for headless mode)
            try:
                hwnd = self._find_cad_window()
                try:
                    if win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"Could not focus window for export: {e}")
            except Exception as e:
                logger.debug(f"Could not find CAD window for focusing: {e}")

            # Use ESC ESC to clear any pending commands
            document.SendCommand("\x1b\x1b")
            time.sleep(0.1)

            # Disable file dialog
            document.SendCommand("FILEDIA 0\n")
            time.sleep(0.1)

            # Execute PNGOUT command with file path
            # Sequence: command, path, selection (all), finish selection
            safe_path = self._sanitize_command_input(filepath_cad)
            document.SendCommand(f"_PNGOUT\n{safe_path}\n_ALL\n\n")

            # Wait briefly for file to be written
            time.sleep(1.5)  # Increased wait for render

            # Re-enable file dialog
            document.SendCommand("FILEDIA 1\n")

            # Verify file was created
            if not os.path.exists(filepath):
                # Try fallback: maybe it didn't like _ALL, try just \n\n
                logger.debug(
                    "PNGOUT with _ALL failed, trying with default selection..."
                )
                safe_path_fallback = self._sanitize_command_input(filepath_cad)
                document.SendCommand(f"_PNGOUT\n{safe_path_fallback}\n\n")
                time.sleep(1.5)

            if not os.path.exists(filepath):
                raise Exception(f"Export file was not created at {filepath}")

            # Convert to base64
            with open(filepath, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

            logger.info(f"View exported to {filepath}")

            return {"path": filepath, "data": encoded_string}

        except Exception as e:
            logger.error(f"Export view failed: {e}")
            raise Exception(f"Failed to export view: {e}")

    def zoom_extents(self) -> bool:
        """Zoom the active viewport to fit all drawing entities via COM.

        Returns:
            True if successful, False otherwise.
        """
        try:
            application = self._get_application("zoom_extents")
            application.ZoomExtents()
            logger.debug("Zoomed to extents")
            return True
        except Exception as e:
            logger.error(f"Failed to zoom extents: {e}")
            return False

    def refresh_view(self) -> bool:
        """Refresh the view using multiple techniques for maximum compatibility.

        Uses a combination of techniques in fallback order:
        1. Application.Refresh() (COM API - no undo/redo impact)
        2. SendCommand with REDRAW (most reliable visual update)
        3. Window click simulation (forces UI update)

        Note: REDRAW command is not wrapped in UNDO to avoid complicating
        the undo/redo stack. If refresh_view is called during user operations,
        the REDRAW will be undone by the user's undo command anyway.

        Returns:
            True if refresh was attempted (best effort approach)
        """
        try:
            application = self._get_application("refresh_view")
            document = self._get_document("refresh_view")

            # Technique 1: COM API Refresh (doesn't affect undo/redo)
            try:
                application.Refresh()
                logger.debug("Refresh: COM Refresh executed")
            except Exception as e:
                logger.debug(f"COM Refresh failed: {e}")

            # Technique 2: Send REDRAW command (most reliable visual update)
            try:
                document.SendCommand("_redraw\n")
                logger.debug("Refresh: REDRAW command sent")
            except Exception as e:
                logger.debug(f"REDRAW command failed: {e}")

            # Technique 3: Simulate click on CAD window (forces UI update)
            self._simulate_autocad_click()

            return True
        except Exception as e:
            logger.debug(f"refresh_view error: {e}")
            return False

    def undo(self, count: int = 1) -> bool:
        """Undo last action(s).

        Args:
            count: Number of operations to undo (default: 1)

        Returns:
            True if successful, False otherwise
        """
        try:
            self._validate_connection()
            if count < 1:
                logger.warning(f"Invalid undo count: {count}. Must be >= 1")
                return False

            app = self._get_application("undo")
            app.ActiveDocument.SendCommand(f"_undo {count}\n")
            logger.info(f"Undo executed ({count} operation(s))")
            return True
        except Exception as e:
            logger.error(f"Failed to undo: {e}")
            return False

    def redo(self, count: int = 1) -> bool:
        """Redo last undone action(s).

        Args:
            count: Number of operations to redo (default: 1)

        Returns:
            True if successful, False otherwise
        """
        try:
            self._validate_connection()
            if count < 1:
                logger.warning(f"Invalid redo count: {count}. Must be >= 1")
                return False

            app = self._get_application("redo")
            app.ActiveDocument.SendCommand(f"_redo {count}\n")
            logger.info(f"Redo executed ({count} operation(s))")
            return True
        except Exception as e:
            logger.error(f"Failed to redo: {e}")
            return False
