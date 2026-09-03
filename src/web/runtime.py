"""Cross-process runtime identity and single-owner dashboard coordination."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from __version__ import __version__

PROCESS_STARTED_AT = time.time()
STUDIO_VERSION = __version__
WORKSPACE_SCHEMA_VERSION = 3


def _runtime_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    return root / "TopoSpatial Studio" / "runtime"


def _source_build_id() -> str:
    """Fingerprint loaded backend and built editor sources at process startup."""
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "server.py",
        root / "web" / "runtime.py",
        root / "web" / "api.py",
        root / "design_engine" / "service.py",
        root / "design_engine" / "orchestrator.py",
        root / "design_engine" / "workspace.py",
        root / "mcp_tools" / "tools" / "design.py",
        root / "mcp_tools" / "tools" / "topology.py",
        root / "topology_engine" / "cad_bridge.py",
        root / "topology_engine" / "engine.py",
        root / "topology_engine" / "navigation.py",
        root / "web" / "static" / "editor" / "index.html",
    ]
    assets = root / "web" / "static" / "editor" / "assets"
    if assets.exists():
        candidates.extend(sorted(path for path in assets.iterdir() if path.is_file()))
    digest = hashlib.sha256()
    digest.update(__version__.encode("utf-8"))
    for path in candidates:
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return "sha256:" + digest.hexdigest()


BUILD_ID = os.environ.get("TOPOSPATIAL_BUILD_ID") or _source_build_id()


def runtime_metadata() -> Dict[str, Any]:
    """Return immutable identity fields for this loaded backend process."""
    return {
        "process_id": os.getpid(),
        "started_at": PROCESS_STARTED_AT,
        "studio_version": STUDIO_VERSION,
        "build_id": BUILD_ID,
        "workspace_schema_version": WORKSPACE_SCHEMA_VERSION,
    }


def _state_path(role: str) -> Path:
    safe_role = "".join(ch for ch in role.casefold() if ch.isalnum() or ch in "-_")
    return _runtime_dir() / f"{safe_role}.json"


def write_runtime_state(
    role: str,
    *,
    drawing: str,
    drawing_revision: str,
    graph_revision: str,
) -> Dict[str, Any]:
    """Publish a small atomic cross-process revision heartbeat."""
    payload = {
        **runtime_metadata(),
        "role": role,
        "drawing": drawing,
        "drawing_revision": drawing_revision,
        "graph_revision": graph_revision,
        "updated_at": time.time(),
    }
    directory = _runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = _state_path(role)
    fd, name = tempfile.mkstemp(prefix=f"{role}-{os.getpid()}-", suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        for attempt in range(5):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def read_runtime_state(role: str, *, max_age_seconds: float = 300.0) -> Optional[Dict[str, Any]]:
    """Read a recent revision heartbeat, ignoring corrupt or stale records."""
    try:
        payload = json.loads(_state_path(role).read_text(encoding="utf-8"))
        updated_at = float(payload.get("updated_at", 0.0))
        if time.time() - updated_at > max_age_seconds:
            return None
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


class DashboardOwnership:
    """Hold a one-byte OS lock for one dashboard port."""

    def __init__(self, port: int, directory: Path | None = None) -> None:
        self.port = int(port)
        self.directory = directory or _runtime_dir()
        self.path = self.directory / f"dashboard-{self.port}.lock"
        self._handle: Any | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised on non-Windows CI only
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        metadata = json.dumps(runtime_metadata(), sort_keys=True).encode("utf-8")
        handle.seek(1)
        handle.truncate()
        handle.write(metadata)
        handle.flush()
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on non-Windows CI only
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


def probe_dashboard(host: str, port: int, timeout: float = 0.75) -> Optional[Dict[str, Any]]:
    """Return dashboard health without depending on third-party HTTP clients."""
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/health", timeout=timeout
        ) as response:
            if response.status != 200:
                return None
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None


class DashboardCoordinator:
    """Start one dashboard per port and take over when its owner exits."""

    def __init__(
        self,
        app: Any,
        host: str,
        port: int,
        logger: logging.Logger,
        *,
        poll_seconds: float = 2.0,
        server_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.app = app
        self.host = host
        self.port = int(port)
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.ownership = DashboardOwnership(self.port)
        self._server_factory = server_factory
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._server: Any | None = None
        self._server_thread: threading.Thread | None = None
        self._last_notice: str | None = None

    def start(self) -> None:
        if self._monitor and self._monitor.is_alive():
            return
        self._stop.clear()
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="topospatial-dashboard-coordinator",
            daemon=True,
        )
        self._monitor.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        self._stop.set()
        if self._monitor and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=3.0)
        if self._server is not None:
            self._server.should_exit = True
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5.0)
            if self._server_thread.is_alive():
                self.logger.error("Dashboard shutdown is still pending; retaining ownership until process exit")
                return
        self.ownership.release()

    def _notice(self, key: str, message: str, *, error: bool = False) -> None:
        if self._last_notice == key:
            return
        self._last_notice = key
        (self.logger.error if error else self.logger.info)(message)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            if self.ownership.acquired:
                if self._server_thread and self._server_thread.is_alive():
                    self._stop.wait(self.poll_seconds)
                    continue
                self.ownership.release()

            health = probe_dashboard(self.host, self.port)
            if health is not None:
                remote_build = health.get("build_id")
                if remote_build == BUILD_ID:
                    self._notice(
                        "reused",
                        f"Reusing compatible TopoSpatial dashboard on http://{self.host}:{self.port}",
                    )
                else:
                    self._notice(
                        f"mismatch:{remote_build}",
                        "TopoSpatial dashboard build mismatch on "
                        f"http://{self.host}:{self.port}; restart TopoSpatial/AutoCAD "
                        "to replace the stale dashboard owner.",
                        error=True,
                    )
                self._stop.wait(self.poll_seconds)
                continue

            if self.ownership.acquire():
                self._start_owned_server()
            self._stop.wait(self.poll_seconds)

    def _start_owned_server(self) -> None:
        if self._server_factory is None:
            import uvicorn

            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=3,
            )
            self._server = uvicorn.Server(config)
        else:
            self._server = self._server_factory(
                self.app, host=self.host, port=self.port
            )
        self._server_thread = threading.Thread(
            target=self._server.run,
            name="topospatial-dashboard",
            daemon=True,
        )
        self._server_thread.start()
        self._notice(
            "owner",
            f"Started TopoSpatial dashboard on http://{self.host}:{self.port} "
            f"(pid {os.getpid()}, build {BUILD_ID[:15]})",
        )
