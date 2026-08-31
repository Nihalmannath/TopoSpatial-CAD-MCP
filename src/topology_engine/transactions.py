"""Thread-safe, expiring preview transaction storage."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Dict, List

from .models import PreviewTransaction


class TransactionStore:
    """Hold previewed operations until an explicit apply call."""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl_seconds = int(ttl_seconds)
        self._transactions: Dict[str, PreviewTransaction] = {}
        self._lock = threading.Lock()

    def create(
        self,
        drawing_name: str,
        base_revision: str,
        operations: List[dict],
        diff: List[dict],
        warnings: List[str],
    ) -> PreviewTransaction:
        now = time.time()
        transaction = PreviewTransaction(
            transaction_id=str(uuid.uuid4()),
            drawing_name=drawing_name,
            base_revision=base_revision,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            operations=operations,
            diff=diff,
            warnings=warnings,
        )
        with self._lock:
            self._purge_locked(now)
            self._transactions[transaction.transaction_id] = transaction
        return transaction

    def get(self, transaction_id: str) -> PreviewTransaction:
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            transaction = self._transactions.get(transaction_id)
            if transaction is None:
                raise ValueError(
                    "Unknown or expired transaction_id; preview the changes again"
                )
            return transaction

    def mark_applied(self, transaction_id: str, result: dict) -> PreviewTransaction:
        with self._lock:
            transaction = self._transactions.get(transaction_id)
            if transaction is None:
                raise ValueError("Unknown or expired transaction_id")
            transaction.status = "applied"
            transaction.result = result
            return transaction

    def _purge_locked(self, now: float) -> None:
        expired = [
            transaction_id
            for transaction_id, transaction in self._transactions.items()
            if transaction.expires_at <= now and transaction.status != "applied"
        ]
        for transaction_id in expired:
            self._transactions.pop(transaction_id, None)
