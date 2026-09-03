"""High-level architectural orchestration tool with typed action schemas."""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from adapters.adapter_manager import get_adapter
from design_engine import DesignOrchestrator, DesignRequest

_orchestrator = DesignOrchestrator()
_request_adapter: TypeAdapter[Any] = TypeAdapter(DesignRequest)


def register_design_tools(mcp: Any) -> None:
    """Register the typed high-level ``manage_design`` lifecycle tool."""

    @mcp.tool()
    def manage_design(request: DesignRequest) -> str:
        """
        Plan and execute architectural work with few agent round trips.

        The request is a discriminated object. Set ``action`` to one of:

        - ``inspect``: compact drawing/topology summary.
        - ``get_context``: bounded semantic neighborhood by ID or exact label.
        - ``get_result``: page through a previously truncated context result.
        - ``create``: validate and preview a batch of create changes.
        - ``modify``: validate and preview a mixed change batch.
        - ``validate``: build a local plan without storing a transaction.
        - ``preview``: validate and store a mixed transaction.
        - ``apply``: atomically apply a preview transaction.
        - ``cancel``: discard a pending preview without mutating CAD.
        - ``rollback``: safely undo the latest applied design transaction.
        - ``metrics``: return MCP/CAD/topology/retry/response-size proxies.

        ``create``, ``modify``, and ``preview`` perform local topology analysis,
        dependency resolution, host validation, representation selection, and
        execution-plan construction in one MCP call. They never mutate CAD.
        Use the returned transaction ID with ``apply`` after approval.

        For modifications, never create a new drawing unless explicitly asked.
        Modify the current drawing in place. Resolve affected semantic entities,
        request only their bounded neighborhood, locally plan and batch dependent
        geometry operations, validate locally, and return a compact summary.
        Do not interleave deterministic geometry operations with model calls.
        Reuse cached topology for an unchanged revision, and stop after preview
        whenever approval is required.
        """
        try:
            parsed = (
                request
                if hasattr(request, "model_dump")
                else _request_adapter.validate_python(request)
            )
        except ValidationError as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": {
                        "code": "DESIGN_REQUEST_INVALID",
                        "stage": "schema",
                        "retryable": False,
                        "details": exc.errors(include_url=False),
                        "suggested_action": (
                            "Match the fields required by the selected action; "
                            "do not wrap arrays or objects in JSON strings."
                        ),
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

        adapter = (
            None
            if parsed.action in {"metrics", "get_result"}
            else get_adapter(None, only_if_running=True)
        )
        result = _orchestrator.execute(parsed, adapter)
        indent = 2 if parsed.detail_level == "debug" else None
        separators = None if indent else (",", ":")
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            indent=indent,
            separators=separators,
        )
        task_id = result.get("task_id")
        if task_id:
            _orchestrator.record_response_size(
                str(task_id), len(encoded.encode("utf-8"))
            )
        return encoded
