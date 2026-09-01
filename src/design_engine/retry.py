"""Bounded retry and repeated-failure protection for deterministic operations."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

T = TypeVar("T")


class RetryPolicy:
    """Retry only recognized transient AutoCAD failures, at most once."""

    TRANSIENT_MARKERS = (
        "rpc_e_call_rejected",
        "call was rejected by callee",
        "server busy",
        "application is busy",
    )

    def run(
        self,
        operation: Callable[[], T],
        on_retry: Optional[Callable[[Exception], None]] = None,
    ) -> Tuple[T, int]:
        """Execute once and retry one recognized transient failure."""
        retries = 0
        for attempt in range(2):
            try:
                return operation(), retries
            except Exception as exc:
                transient = any(
                    marker in str(exc).casefold() for marker in self.TRANSIENT_MARKERS
                )
                if not transient or attempt == 1:
                    raise
                retries += 1
                if on_retry is not None:
                    on_retry(exc)
                time.sleep(0.15)
        raise RuntimeError("unreachable")


class FailureMemory:
    """Stop an identical request from repeating the same failure endlessly."""

    def __init__(self) -> None:
        """Initialize empty request-failure memory."""
        self._items: Dict[str, Tuple[str, int]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def request_key(request: Dict[str, Any]) -> str:
        """Hash a normalized request into a stable compact key."""
        encoded = json.dumps(
            request, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20]

    @staticmethod
    def error_fingerprint(code: str, stage: str, details: str) -> str:
        """Hash a structured error into a stable agent-visible fingerprint."""
        encoded = f"{code}|{stage}|{details}".encode("utf-8")
        return "err_" + hashlib.sha256(encoded).hexdigest()[:16]

    def record(self, request_key: str, fingerprint: str) -> int:
        """Record a failure and return its consecutive occurrence count."""
        with self._lock:
            previous, count = self._items.get(request_key, ("", 0))
            count = count + 1 if previous == fingerprint else 1
            self._items[request_key] = (fingerprint, count)
            return count

    def blocked(self, request_key: str) -> Tuple[str, int] | None:
        """Return the repeated failure after two identical attempts."""
        with self._lock:
            fingerprint, count = self._items.get(request_key, ("", 0))
            if fingerprint and count >= 2:
                return fingerprint, count
            return None

    def clear(self, request_key: str) -> None:
        """Forget failure history after a successful request."""
        with self._lock:
            self._items.pop(request_key, None)
