"""Thread ownership regression tests for CAD adapter registries."""

from __future__ import annotations

import threading

from adapters.adapter_manager import AdapterRegistry
from mcp_tools.decorators import AdapterContext


class FakeAdapter:
    def __init__(self, owner: int) -> None:
        self.owner = owner

    def is_connected(self) -> bool:
        return True


def test_adapter_context_is_thread_local() -> None:
    context = AdapterContext()
    barrier = threading.Barrier(2)
    values = []

    def worker(value):
        context.set_current_adapter(value)
        barrier.wait()
        values.append(context.get_current_adapter())

    threads = [threading.Thread(target=worker, args=(value,)) for value in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(values) == ["a", "b"]


def test_adapter_registry_shares_single_adapter_between_threads(monkeypatch) -> None:
    """Requirement 6 & 11: AdapterRegistry coordinates across threads and shares a single adapter."""
    registry = AdapterRegistry()
    barrier = threading.Barrier(2)
    adapters = []
    detect_calls = []

    def fake_detect(only_if_running=True, allow_launch=False):
        detect_calls.append(threading.get_ident())
        registry._adapter = FakeAdapter(threading.get_ident())
        registry._cad_type = "autocad"

    monkeypatch.setattr(registry, "_auto_detect_internal", fake_detect)

    def worker():
        barrier.wait()
        adapters.append(registry.get_adapter(only_if_running=True))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(adapters) == 2
    # Both threads receive the SAME coordinated adapter instance
    assert adapters[0] is adapters[1]
    # Auto-detection is executed only once, preventing duplicate adapter/process creation
    assert len(detect_calls) == 1
