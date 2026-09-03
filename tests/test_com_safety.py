"""Comprehensive process, COM, licensing, and crash safety tests for AutoCAD MCP."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from adapters import (
    AutoCADAdapter,
    CADBusyCircuitBreaker,
    COMWorker,
    get_com_worker,
    is_com_busy_error,
    is_com_not_running_error,
    run_com,
)
from adapters.adapter_manager import AdapterRegistry, get_adapter
from core.exceptions import CADBusyError, CADConnectionError
from design_engine import DesignOrchestrator
from design_engine.models import InspectDesignRequest
from mcp_tools.tools.session import _connect, _start


class FakeCOMError(Exception):
    """Simulates pywintypes.com_error with args tuple."""

    def __init__(self, hresult: int, message: str) -> None:
        super().__init__(hresult, message)
        self.args = (hresult, message, None, None)
        self.hresult = hresult


def test_default_connection_parameters():
    """Requirement 1 & 2: Normal connection defaults to only_if_running=True, allow_launch=False."""
    adapter = AutoCADAdapter("autocad")
    # Verify signature defaults
    import inspect
    sig = inspect.signature(adapter.connect)
    assert sig.parameters["only_if_running"].default is True
    assert sig.parameters["allow_launch"].default is False

    sig_mgr = inspect.signature(get_adapter)
    assert sig_mgr.parameters["only_if_running"].default is True
    assert sig_mgr.parameters["allow_launch"].default is False


def test_not_running_does_not_call_dispatch():
    """Requirement 1 & 3: When AutoCAD is not running, connect() fails without Dispatch()."""
    adapter = AutoCADAdapter("autocad")
    with (
        patch.object(adapter, "_get_active_com_object", side_effect=CADConnectionError("autocad", "CAD not running")),
        patch("adapters.mixins.connection_mixin.win32com.client.Dispatch") as mock_dispatch,
    ):
        result = adapter.connect(only_if_running=True, allow_launch=False)
        assert result is False
        mock_dispatch.assert_not_called()


def test_busy_autocad_results_in_cad_busy_not_dispatch():
    """Requirement 5 & 12: A busy AutoCAD raises CADBusyError and NEVER calls Dispatch()."""
    adapter = AutoCADAdapter("autocad")
    cb = get_com_worker().circuit_breaker
    cb.reset()

    # RPC_E_CALL_REJECTED (0x80010001, -2147418111)
    busy_exc = FakeCOMError(-2147418111, "Call was rejected by callee.")

    with (
        patch.object(adapter, "_get_active_com_object", side_effect=busy_exc),
        patch("adapters.mixins.connection_mixin.win32com.client.Dispatch") as mock_dispatch,
    ):
        with pytest.raises(CADBusyError) as exc_info:
            adapter.connect(only_if_running=True, allow_launch=False)

        assert "busy" in str(exc_info.value).lower() or "rejected" in str(exc_info.value).lower()
        mock_dispatch.assert_not_called()


def test_error_discrimination_busy_vs_not_running():
    """Requirement 5: Distinguish transient busy/modal states from truly unstarted states."""
    # Transient busy conditions (AutoCAD IS running)
    assert is_com_busy_error(FakeCOMError(-2147418111, "Call was rejected by callee")) is True
    assert is_com_busy_error(FakeCOMError(-2147417846, "Application is busy")) is True
    assert is_com_busy_error(Exception("The message filter indicated that the application is busy")) is True
    assert is_com_busy_error(CADBusyError("autocad", "modal dialog open")) is True

    # Busy states must NEVER be reported as not running
    assert is_com_not_running_error(FakeCOMError(-2147418111, "Call was rejected by callee")) is False
    assert is_com_not_running_error(Exception("application is busy")) is False

    # Truly not running states (AutoCAD is NOT in Running Object Table)
    not_running_exc = FakeCOMError(-2147221021, "Operation unavailable (MK_E_UNAVAILABLE)")
    assert is_com_not_running_error(not_running_exc) is True
    assert is_com_busy_error(not_running_exc) is False


def test_circuit_breaker_trips_and_fast_fails():
    """Requirement 7: CADBusyCircuitBreaker trips on repeated busy errors and fast-fails."""
    breaker = CADBusyCircuitBreaker(failure_threshold=3, cooldown_seconds=2.0)
    breaker.reset()

    assert breaker.is_open is False
    # Normal checks pass without error
    breaker.check("autocad")

    # Record 2 failures (threshold not reached)
    breaker.record_busy("autocad", "attempt 1")
    breaker.record_busy("autocad", "attempt 2")
    assert breaker.is_open is False

    # 3rd failure trips the breaker
    breaker.record_busy("autocad", "attempt 3")
    assert breaker.is_open is True

    # Immediate check fast-fails with CADBusyError
    with pytest.raises(CADBusyError) as exc_info:
        breaker.check("autocad")
    assert "Circuit breaker OPEN" in str(exc_info.value)

    # Reset closes the breaker
    breaker.record_success()
    assert breaker.is_open is False
    breaker.check("autocad")  # passes


def test_com_worker_serializes_calls():
    """Requirement 6: All COM calls execute sequentially on the single STA worker thread."""
    worker = COMWorker.get_instance()
    execution_threads = []
    concurrency_counter = 0
    max_concurrent_observed = 0
    lock = threading.Lock()

    def sample_task(val: int) -> int:
        nonlocal concurrency_counter, max_concurrent_observed
        with lock:
            concurrency_counter += 1
            if concurrency_counter > max_concurrent_observed:
                max_concurrent_observed = concurrency_counter
        execution_threads.append(threading.get_ident())
        time.sleep(0.02)
        with lock:
            concurrency_counter -= 1
        return val * 2

    # Spawn 5 caller threads
    results = []
    threads = []
    for i in range(5):
        t = threading.Thread(target=lambda val=i: results.append(worker.run(sample_task, val)))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # All executions occurred on the single worker thread
    assert len(execution_threads) == 5
    assert len(set(execution_threads)) == 1
    assert execution_threads[0] == worker.worker_thread_id

    # No concurrent execution occurred inside the worker
    assert max_concurrent_observed == 1
    assert sorted(results) == [0, 2, 4, 6, 8]


def test_concurrent_mcp_calls_cannot_create_duplicate_processes():
    """Requirement 11: Concurrent MCP calls cannot create another AutoCAD process."""
    AdapterRegistry.reset()
    registry = AdapterRegistry.get_instance()

    dispatch_calls = []
    connect_calls = []

    class MockAdapter:
        def __init__(self):
            self.cad_type = "autocad"
            self._connected = False

        def is_connected(self):
            return self._connected

        def connect(self, only_if_running=True, allow_launch=False):
            connect_calls.append(threading.get_ident())
            if not allow_launch:
                time.sleep(0.03)
                self._connected = True
                return True
            dispatch_calls.append(threading.get_ident())
            return True

    mock_inst = MockAdapter()

    with patch("adapters.AutoCADAdapter", return_value=mock_inst):
        barrier = threading.Barrier(5)
        adapters = []

        def worker():
            barrier.wait()
            ad = registry.get_adapter(only_if_running=True, allow_launch=False)
            adapters.append(ad)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 5 threads received the exact same adapter instance
        assert len(adapters) == 5
        for ad in adapters:
            assert ad is mock_inst

        # Dispatch was NEVER called
        assert len(dispatch_calls) == 0


def test_session_connect_attaches_only_and_start_allows_launch():
    """Requirement 4: Session connect attaches only; session start explicitly launches."""
    with (
        patch("mcp_tools.tools.session.get_adapter") as mock_get_adapter,
        patch("adapters.adapter_manager.get_active_cad_type", return_value="autocad"),
        patch("mcp_tools.tools.session._refresh_cache_safe"),
    ):
        mock_get_adapter.return_value = MagicMock()

        # session connect
        res_connect = _connect({})
        assert res_connect["success"] is True
        mock_get_adapter.assert_called_with(only_if_running=True, allow_launch=False)

        # session start
        res_start = _start({})
        assert res_start["success"] is True
        mock_get_adapter.assert_called_with(only_if_running=False, allow_launch=True)


def test_session_connect_failure_does_not_launch():
    """Requirement 4: Session connect failure suggests 'start' and does not launch."""
    with patch(
        "mcp_tools.tools.session.get_adapter",
        side_effect=CADConnectionError("autocad", "CAD application is not running"),
    ):
        res = _connect({})
        assert res["success"] is False
        assert "CAD application is not running" in res["detail"]
        assert "Use session 'start'" in res["detail"]


def test_design_orchestrator_does_not_reconnect_during_apply():
    """Requirement 8: DesignOrchestrator does not reconnect AutoCAD halfway through a transaction."""
    orchestrator = DesignOrchestrator()
    adapter = MagicMock()
    adapter.cad_type = "autocad"
    adapter.connect = MagicMock()

    # When an operation fails during apply (allow_reconnect=False default)
    failing_op = MagicMock(side_effect=CADBusyError("autocad", "modal popup"))

    with pytest.raises(CADBusyError):
        orchestrator._run_cad(adapter, failing_op, allow_reconnect=False)

    # adapter.connect was NOT called
    adapter.connect.assert_not_called()


def test_design_orchestrator_fails_safely_on_cad_busy():
    """Requirement 9: DesignOrchestrator maps CAD_BUSY cleanly and reports no mutation."""
    orchestrator = DesignOrchestrator()
    adapter = MagicMock()
    adapter.cad_type = "autocad"

    with patch.object(orchestrator, "_snapshot_and_analysis", side_effect=CADBusyError("autocad", "application busy")):
        req = MagicMock(action="inspect", scope="all", task_id="task_123")
        req.model_dump.return_value = {"action": "inspect", "scope": "all", "task_id": "task_123"}
        result = orchestrator.execute(req, adapter)

        assert result["success"] is False
        assert result["error"]["code"] == "CAD_BUSY"
        assert result["error"]["retryable"] is False
        assert "busy" in result["error"]["details"].lower()


def test_validate_connection_busy_raises_cad_busy_without_reconnect():
    """Requirement 5 & 9: _validate_connection raises CADBusyError when busy, without reconnecting."""
    adapter = AutoCADAdapter("autocad")
    adapter.application = MagicMock()
    type(adapter.application).Visible = property(
        fget=MagicMock(side_effect=FakeCOMError(-2147418111, "Call was rejected by callee."))
    )

    with patch.object(adapter, "connect") as mock_connect:
        with pytest.raises(CADBusyError):
            adapter._validate_connection()

        # Must NOT call connect() when busy
        mock_connect.assert_not_called()
