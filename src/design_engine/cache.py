"""Small revision-keyed caches for topology analysis and bounded query results."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from topology_engine.models import DrawingSnapshot


class AnalysisCache:
    """Cache pure topology analysis by drawing fingerprint."""

    def __init__(self, max_entries: int = 8) -> None:
        """Initialize a bounded least-recently-used cache."""
        self.max_entries = max(1, int(max_entries))
        self._items: OrderedDict[
            Tuple[str, str], Tuple[DrawingSnapshot, Dict[str, Any]]
        ] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(
        self, snapshot: DrawingSnapshot
    ) -> Optional[Tuple[DrawingSnapshot, Dict[str, Any]]]:
        """Return a defensive copy for the exact drawing revision."""
        key = (snapshot.drawing_name.casefold(), snapshot.revision)
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item[0], copy.deepcopy(item[1])

    def put(self, snapshot: DrawingSnapshot, analysis: Dict[str, Any]) -> None:
        """Store analysis for an immutable snapshot revision."""
        key = (snapshot.drawing_name.casefold(), snapshot.revision)
        with self._lock:
            self._items[key] = (snapshot, copy.deepcopy(analysis))
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def invalidate(self, drawing_name: str) -> None:
        """Remove every cached revision for one drawing."""
        drawing_key = drawing_name.casefold()
        with self._lock:
            for key in [key for key in self._items if key[0] == drawing_key]:
                self._items.pop(key, None)

    def stats(self) -> Dict[str, int]:
        """Return cache hit, miss, and entry counts."""
        with self._lock:
            return {
                "entries": len(self._items),
                "hits": self.hits,
                "misses": self.misses,
            }


@dataclass
class StoredResult:
    """One expiring context-query result."""

    result_id: str
    created_at: float
    value: Dict[str, Any]


class ResultStore:
    """Store oversized bounded-query results for optional follow-up retrieval."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        """Initialize the expiring result store."""
        self.ttl_seconds = int(ttl_seconds)
        self._items: Dict[str, StoredResult] = {}
        self._lock = threading.Lock()

    def put(self, value: Dict[str, Any]) -> str:
        """Store a defensive copy and return its opaque identifier."""
        now = time.time()
        result_id = f"result_{uuid.uuid4().hex}"
        with self._lock:
            self._purge(now)
            self._items[result_id] = StoredResult(result_id, now, copy.deepcopy(value))
        return result_id

    def get(self, result_id: str) -> Dict[str, Any]:
        """Read a non-expired result by identifier."""
        now = time.time()
        with self._lock:
            self._purge(now)
            item = self._items.get(result_id)
            if item is None:
                raise ValueError("Unknown or expired result_id")
            return copy.deepcopy(item.value)

    def _purge(self, now: float) -> None:
        for result_id in [
            result_id
            for result_id, item in self._items.items()
            if item.created_at + self.ttl_seconds <= now
        ]:
            self._items.pop(result_id, None)
