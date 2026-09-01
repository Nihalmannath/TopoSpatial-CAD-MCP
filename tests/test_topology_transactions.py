"""Tests for expiring, idempotent topology preview transactions."""

from __future__ import annotations

import pytest

from topology_engine import TransactionStore


def test_pending_transaction_expires(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr("topology_engine.transactions.time.time", lambda: now)
    store = TransactionStore(ttl_seconds=600)
    transaction = store.create("room.dwg", "sha256:a", [], [], [])

    now = 1601.0
    with pytest.raises(ValueError, match="expired"):
        store.get(transaction.transaction_id)


def test_applied_transaction_keeps_its_idempotent_result(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr("topology_engine.transactions.time.time", lambda: now)
    store = TransactionStore(ttl_seconds=600)
    transaction = store.create("room.dwg", "sha256:a", [], [], [])
    store.mark_applied(transaction.transaction_id, {"revision": "sha256:b"})

    now = 2000.0
    stored = store.get(transaction.transaction_id)

    assert stored.status == "applied"
    assert stored.result == {"revision": "sha256:b"}


def test_pending_transaction_can_be_cancelled_without_apply() -> None:
    store = TransactionStore(ttl_seconds=600)
    transaction = store.create("room.dwg", "sha256:a", [], [], [])

    cancelled = store.cancel(transaction.transaction_id)

    assert cancelled.status == "cancelled"
    with pytest.raises(ValueError, match="Cancelled transactions"):
        store.mark_applied(transaction.transaction_id, {})
