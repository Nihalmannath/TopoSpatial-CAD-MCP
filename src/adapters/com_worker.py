"""
Dedicated serialized COM execution worker and CAD_BUSY circuit breaker.

Enforces:
1. Single-Threaded Apartment (STA) serialization: all AutoCAD COM calls are
   executed sequentially on a single dedicated background thread.
2. COM error discrimination: distinguishes transient busy/modal states
   (RPC_E_CALL_REJECTED, SERVER_BUSY) from truly unstarted states (MK_E_UNAVAILABLE).
3. CAD_BUSY circuit breaker with bounded retry and backoff: stops call storms
   when AutoCAD is displaying a modal dialog, running a long command, or busy.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Set, Tuple, TypeVar

from core.exceptions import CADBusyError, CADConnectionError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Standard COM HRESULT error codes
RPC_E_CALL_REJECTED = -2147418111          # 0x80010001: Call was rejected by callee
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A: Server call retry later / app busy
RPC_E_SERVERCALL_REJECTED = -2147417845    # 0x8001010B: Server call rejected
RPC_E_CANTCALLOUT_ININPUTSYNCCALL = -2147417843  # 0x8001010D
RPC_E_DISCONNECTED = -2147417848           # 0x80010108: Object disconnected
MK_E_UNAVAILABLE = -2147221021             # 0x800401E3: Operation unavailable (not in ROT)
CO_E_CLASSSTRING = -2147221005             # 0x800401F3: Invalid class string / ProgID

BUSY_HRESULTS: Set[int] = {
    RPC_E_CALL_REJECTED,
    RPC_E_SERVERCALL_RETRYLATER,
    RPC_E_SERVERCALL_REJECTED,
    RPC_E_CANTCALLOUT_ININPUTSYNCCALL,
}

BUSY_STRING_MARKERS: Tuple[str, ...] = (
    "rpc_e_call_rejected",
    "call was rejected by callee",
    "server busy",
    "application is busy",
    "servercall_retrylater",
    "the message filter indicated that the application is busy",
    "cad_busy",
)

NOT_RUNNING_HRESULTS: Set[int] = {
    MK_E_UNAVAILABLE,
    CO_E_CLASSSTRING,
}

NOT_RUNNING_STRING_MARKERS: Tuple[str, ...] = (
    "operation unavailable",
    "invalid class string",
    "mk_e_unavailable",
    "co_e_classstring",
    "cad application is not running",
)


def get_hresult(exc: Exception) -> Optional[int]:
    """Extract integer HRESULT from pywintypes.com_error or exception attributes."""
    val = None
    if hasattr(exc, "hresult") and isinstance(exc.hresult, int):
        val = exc.hresult
    elif hasattr(exc, "args") and exc.args and isinstance(exc.args[0], int):
        val = exc.args[0]
    if val is not None:
        if val > 0x7FFFFFFF:
            val -= 0x100000000
        return val
    return None


def is_com_busy_error(exc: Exception) -> bool:
    """Check if an exception indicates AutoCAD is running but busy or modal.

    A busy state means AutoCAD IS RUNNING. It must NEVER be interpreted as
    'AutoCAD is not running' or trigger Dispatch() process creation.
    """
    if isinstance(exc, CADBusyError):
        return True
    hr = get_hresult(exc)
    if hr is not None and hr in BUSY_HRESULTS:
        return True
    err_str = str(exc).casefold()
    return any(marker in err_str for marker in BUSY_STRING_MARKERS)


def is_com_not_running_error(exc: Exception) -> bool:
    """Check if an exception indicates AutoCAD is truly not running.

    Returns True ONLY when ROT lookup confirms the object does not exist.
    Never returns True for busy, rejected, or modal states.
    """
    if is_com_busy_error(exc):
        return False
    hr = get_hresult(exc)
    if hr is not None and hr in NOT_RUNNING_HRESULTS:
        return True
    err_str = str(exc).casefold()
    return any(marker in err_str for marker in NOT_RUNNING_STRING_MARKERS)


class CADBusyCircuitBreaker:
    """Circuit breaker for AutoCAD COM operations.

    States:
    - CLOSED: Normal operation, calls proceed.
    - OPEN: AutoCAD has rejected multiple calls (e.g. modal dialog, busy command).
      Calls fast-fail with CADBusyError without touching COM.
    - HALF_OPEN: Cooldown period has elapsed; next call probes CAD state.
    """

    def __init__(
        self,
        failure_threshold: Optional[int] = None,
        cooldown_seconds: Optional[float] = None,
        backoff_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        cfg_threshold = 3
        cfg_cooldown = 5.0
        cfg_backoff = 0.2
        cfg_retries = 2
        try:
            from core.config import get_config
            cfg = get_config()
            cb_cfg = getattr(cfg, "circuit_breaker", None)
            if cb_cfg is not None:
                cfg_threshold = getattr(cb_cfg, "failure_threshold", cfg_threshold)
                cfg_cooldown = getattr(cb_cfg, "cooldown_seconds", cfg_cooldown)
                cfg_backoff = getattr(cb_cfg, "backoff_seconds", cfg_backoff)
                cfg_retries = getattr(cb_cfg, "max_retries", cfg_retries)
        except Exception:
            pass

        self.failure_threshold = failure_threshold if failure_threshold is not None else cfg_threshold
        self.cooldown_seconds = cooldown_seconds if cooldown_seconds is not None else cfg_cooldown
        self.backoff_seconds = backoff_seconds if backoff_seconds is not None else cfg_backoff
        self.max_retries = max_retries if max_retries is not None else cfg_retries

        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._is_open = False
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if not self._is_open:
                return False
            # Check if cooldown has elapsed
            if time.monotonic() - self._last_failure_time >= self.cooldown_seconds:
                return False
            return True

    def check(self, cad_type: str = "autocad") -> None:
        """Raise CADBusyError immediately if circuit is open and cooling down."""
        with self._lock:
            if not self._is_open:
                return
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed < self.cooldown_seconds:
                remaining = self.cooldown_seconds - elapsed
                raise CADBusyError(
                    cad_type,
                    f"Circuit breaker OPEN: CAD application is busy or in a modal dialog. "
                    f"Fast-failing for {remaining:.1f}s to avoid license and COM contention.",
                )
            # Cooldown passed -> transition to probe
            logger.info(
                f"Circuit breaker cooldown ({self.cooldown_seconds}s) elapsed; "
                f"allowing probe call to {cad_type}."
            )
            self._is_open = False

    def record_success(self) -> None:
        """Reset failure counters after a successful CAD call."""
        with self._lock:
            self._consecutive_failures = 0
            self._is_open = False

    def record_busy(self, cad_type: str = "autocad", reason: str = "") -> None:
        """Record a busy failure and trip the circuit breaker if threshold reached."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()
            if self._consecutive_failures >= self.failure_threshold:
                self._is_open = True
                logger.warning(
                    f"CAD_BUSY Circuit breaker TRIPPED for {cad_type} after "
                    f"{self._consecutive_failures} consecutive busy errors: {reason}"
                )

    def reset(self) -> None:
        """Explicitly reset the circuit breaker state."""
        with self._lock:
            self._consecutive_failures = 0
            self._last_failure_time = 0.0
            self._is_open = False


class COMWorker:
    """Dedicated single-threaded apartment (STA) execution worker for AutoCAD COM.

    Ensures that ALL COM interactions occur sequentially on a single, persistent
    STA thread. This eliminates:
    1. Concurrent RPC calls colliding in AutoCAD's STA message pump.
    2. Multi-threaded proxy leakage and apartment violations.
    3. Thread-local connection races that spawn duplicate acad.exe instances.
    """

    _instance: Optional["COMWorker"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._task_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._circuit_breaker = CADBusyCircuitBreaker()
        self._running = False
        self._init_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "COMWorker":
        """Get or initialize the singleton COMWorker."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    worker = cls()
                    worker.start()
                    cls._instance = worker
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Stop and reset the singleton worker (used in tests)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.stop()
                cls._instance = None

    @property
    def circuit_breaker(self) -> CADBusyCircuitBreaker:
        return self._circuit_breaker

    @property
    def worker_thread_id(self) -> Optional[int]:
        return self._thread_id

    def is_current_thread(self) -> bool:
        """Check if current thread is the dedicated COM worker thread."""
        return self._thread_id is not None and threading.get_ident() == self._thread_id

    def start(self) -> None:
        """Start the background STA worker thread."""
        with self._init_lock:
            if self._running and self._thread is not None and self._thread.is_alive():
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="AutoCAD-COMWorker-STA",
                daemon=True,
            )
            started_event = threading.Event()
            self._thread.start()
            # Enqueue ping to guarantee thread ID is initialized before start() returns
            self._task_queue.put((lambda: True, (), {}, started_event, {}))
            started_event.wait(timeout=5.0)

    def stop(self) -> None:
        """Stop the background STA worker thread."""
        with self._init_lock:
            if not self._running:
                return
            self._running = False
            self._task_queue.put(None)  # Sentinel
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)
            self._thread = None
            self._thread_id = None
            self._circuit_breaker.reset()

    def _worker_loop(self) -> None:
        """Main loop of the dedicated STA worker thread."""
        self._thread_id = threading.get_ident()
        # Initialize COM in Single-Threaded Apartment on Windows
        if sys.platform == "win32":
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception as e:
                logger.debug(f"COMWorker CoInitialize: {e}")

        try:
            while self._running:
                try:
                    task = self._task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if task is None:
                    break

                fn, args, kwargs, done_event, container = task
                try:
                    res = fn(*args, **kwargs)
                    container["result"] = res
                    self._circuit_breaker.record_success()
                except Exception as exc:
                    container["error"] = exc
                    if is_com_busy_error(exc):
                        self._circuit_breaker.record_busy(reason=str(exc))
                finally:
                    done_event.set()
                    self._task_queue.task_done()
        finally:
            if sys.platform == "win32":
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception as e:
                    logger.debug(f"COMWorker CoUninitialize: {e}")

    def run(
        self,
        fn: Callable[..., T],
        *args: Any,
        timeout: Optional[float] = 60.0,
        cad_type: str = "autocad",
        retry_busy: bool = True,
        **kwargs: Any,
    ) -> T:
        """Execute a function on the dedicated COM worker thread.

        - Re-entrant: if called from within the worker thread, executes directly.
        - Cross-thread: queues task to worker and blocks until completed or timeout.
        - Circuit breaker: fast-fails with CADBusyError if AutoCAD is unresponsive.
        - Bounded backoff: retries transient busy errors up to max_retries before tripping.
        """
        # Re-entrant execution
        if self.is_current_thread():
            self._circuit_breaker.check(cad_type)
            try:
                res = fn(*args, **kwargs)
                self._circuit_breaker.record_success()
                return res
            except Exception as exc:
                if is_com_busy_error(exc):
                    self._circuit_breaker.record_busy(cad_type, str(exc))
                    if not isinstance(exc, CADBusyError):
                        raise CADBusyError(cad_type, str(exc)) from exc
                raise

        # Caller on another thread: ensure worker is alive
        if not self._running or self._thread is None or not self._thread.is_alive():
            self.start()

        max_attempts = self._circuit_breaker.max_retries if retry_busy else 1
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            # Check circuit breaker before each attempt
            self._circuit_breaker.check(cad_type)

            done_event = threading.Event()
            container: Dict[str, Any] = {}
            self._task_queue.put((fn, args, kwargs, done_event, container))

            if not done_event.wait(timeout=timeout):
                raise TimeoutError(
                    f"COM operation timed out after {timeout}s on worker thread"
                )

            if "error" in container:
                exc = container["error"]
                last_error = exc
                if is_com_busy_error(exc):
                    if attempt < max_attempts - 1:
                        backoff = self._circuit_breaker.backoff_seconds * (2 ** attempt)
                        time.sleep(backoff)
                        continue
                    if not isinstance(exc, CADBusyError):
                        raise CADBusyError(cad_type, str(exc)) from exc
                raise exc

            return container["result"]

        if last_error is not None:
            raise last_error
        raise RuntimeError("COM execution failed unexpectedly")


# Module-level convenience functions
def get_com_worker() -> COMWorker:
    """Return the global COMWorker instance."""
    return COMWorker.get_instance()


def run_com(
    fn: Callable[..., T],
    *args: Any,
    timeout: Optional[float] = 60.0,
    cad_type: str = "autocad",
    retry_busy: bool = True,
    **kwargs: Any,
) -> T:
    """Run a callable on the serialized COM worker."""
    return get_com_worker().run(
        fn, *args, timeout=timeout, cad_type=cad_type, retry_busy=retry_busy, **kwargs
    )
