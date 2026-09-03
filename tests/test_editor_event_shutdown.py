"""An idle browser must not keep a departing dashboard owner alive."""

import asyncio
from types import SimpleNamespace

import pytest

from web import api
from design_engine.service import EditorEventBroker


def test_editor_event_includes_browser_contract_and_legacy_alias():
    broker = EditorEventBroker()
    event = broker.publish("cad.changed", {"drawing_name": "house.dwg"})
    assert event["event_type"] == event["type"] == "cad.changed"
    assert broker.since(0) == [event]


@pytest.mark.asyncio
async def test_idle_editor_disconnect_exits_event_stream(monkeypatch):
    class Socket:
        client = SimpleNamespace(host="127.0.0.1")
        accepted = False

        async def accept(self):
            self.accepted = True

        async def receive(self):
            return {"type": "websocket.disconnect", "code": 1001}

    monkeypatch.setattr(api.application_service.events, "since", lambda sequence: [])
    socket = Socket()
    await asyncio.wait_for(api.api_editor_events(socket, api._editor_session_token), 1)
    assert socket.accepted
