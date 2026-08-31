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


def test_adapter_registry_does_not_share_adapters_between_threads(monkeypatch) -> None:
    registry = AdapterRegistry()
    barrier = threading.Barrier(2)
    adapters = []

    def fake_detect(only_if_running=False):
        del only_if_running
        registry._adapter = FakeAdapter(threading.get_ident())
        registry._cad_type = "autocad"

    monkeypatch.setattr(registry, "_auto_detect_internal", fake_detect)

    def worker():
        barrier.wait()
        adapters.append(registry.get_adapter())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(adapters) == 2
    assert adapters[0] is not adapters[1]
    assert adapters[0].owner != adapters[1].owner
