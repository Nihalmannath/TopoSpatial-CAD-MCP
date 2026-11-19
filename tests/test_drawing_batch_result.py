"""Focused checks for drawing batch input and completion reporting."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from mcp_tools.tools import drawing


def test_json_polyline_accepts_semicolon_points(monkeypatch) -> None:
    adapter = MagicMock()
    adapter.draw_polyline.return_value = "A1"
    monkeypatch.setattr(drawing, "get_current_adapter", lambda: adapter)

    assert (
        drawing._draw_polyline(
            {"points": "0,0;1000,0;1000,1000", "closed": True}
        )
        == "A1"
    )
    assert adapter.draw_polyline.call_args.args[0] == [
        (0.0, 0.0),
        (1000.0, 0.0),
        (1000.0, 1000.0),
    ]


def test_arc_accepts_full_turn_endpoint(monkeypatch) -> None:
    adapter = MagicMock()
    adapter.draw_arc.return_value = "A2"
    monkeypatch.setattr(drawing, "get_current_adapter", lambda: adapter)

    drawing._draw_arc(
        {"center": "0,0", "radius": 500, "start_angle": 270, "end_angle": 360}
    )

    assert adapter.draw_arc.call_args.args[3] == 0.0


def test_partial_batch_reports_failure(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        drawing,
        "cad_tool",
        lambda _mcp, _name: lambda func: captured.setdefault("tool", func),
    )
    adapter = MagicMock()
    monkeypatch.setattr(drawing, "get_current_adapter", lambda: adapter)
    monkeypatch.setitem(
        drawing.ENTITY_DISPATCH, "line", (lambda _spec: "A3", ["start", "end"])
    )
    drawing.register_drawing_tools(None)

    result = json.loads(
        captured["tool"](
            [
                {"type": "line", "start": "0,0", "end": "1,0"},
                {"type": "unknown"},
            ]
        )
    )

    assert result["created"] == 1
    assert result["failed"] == 1
    assert result["failed_indices"] == [1]
    assert result["success"] is False
    assert result["partial"] is True
