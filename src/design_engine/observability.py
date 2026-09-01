"""Task-level proxy metrics for MCP round trips and deterministic local work."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Dict


@dataclass
class TaskMetrics:
    """Metrics available locally without claiming model token usage."""

    mcp_calls: int = 0
    cad_operations: int = 0
    topology_operations: int = 0
    retries: int = 0
    failures: int = 0
    response_bytes: int = 0
    execution_time_ms: float = 0.0
    entities_inspected: int = 0
    entities_modified: int = 0


class MetricsStore:
    """Thread-safe per-task metrics accumulation."""

    def __init__(self) -> None:
        """Initialize an empty metrics registry."""
        self._items: Dict[str, TaskMetrics] = {}
        self._lock = threading.Lock()

    def add(self, task_id: str, **deltas: float | int) -> None:
        """Add numeric deltas to one task."""
        with self._lock:
            metrics = self._items.setdefault(task_id, TaskMetrics())
            for name, value in deltas.items():
                if not hasattr(metrics, name):
                    raise ValueError(f"Unknown metric '{name}'")
                setattr(metrics, name, getattr(metrics, name) + value)

    def snapshot(self, task_id: str) -> Dict[str, float | int]:
        """Return a serializable metrics snapshot."""
        with self._lock:
            return asdict(self._items.setdefault(task_id, TaskMetrics()))
