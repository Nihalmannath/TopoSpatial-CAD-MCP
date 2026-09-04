"""Typed contracts for high-level architectural design orchestration."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from topology_engine.models import TopologyChange

DetailLevel = Literal["summary", "normal", "detailed", "debug"]


class ContextBudget(BaseModel):
    """Limits that keep semantic context inside an agent-friendly budget."""

    model_config = ConfigDict(extra="forbid")

    max_entities: int = Field(default=25, ge=1, le=500)
    max_neighbors: int = Field(default=12, ge=1, le=100)
    graph_depth: int = Field(default=1, ge=0, le=5)
    include_geometry: bool = False
    include_metadata: bool = True


class _RequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    detail_level: DetailLevel = "summary"


class InspectDesignRequest(_RequestBase):
    """Read a compact description of the current drawing and semantic graph."""

    action: Literal["inspect"]
    scope: Literal["all", "selected"] = "all"
    budget: ContextBudget = Field(default_factory=ContextBudget)


class GetContextDesignRequest(_RequestBase):
    """Read a bounded semantic neighborhood around a room or element."""

    action: Literal["get_context"]
    entity: str = Field(min_length=1, max_length=512)
    scope: Literal["all", "selected"] = "all"
    budget: ContextBudget = Field(default_factory=ContextBudget)


class GetResultDesignRequest(_RequestBase):
    """Read one page of a previously truncated semantic context result."""

    action: Literal["get_result"]
    result_id: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    max_entities: int = Field(default=25, ge=1, le=100)


class _ChangeRequestBase(_RequestBase):
    base_revision: str = Field(min_length=8)
    changes: List[TopologyChange] = Field(min_length=1, max_length=1000)


class PlanDesignRequest(_ChangeRequestBase):
    """Validate or preview a high-level architectural change batch."""

    action: Literal["create", "modify", "validate", "preview"]

    @model_validator(mode="after")
    def validate_action_contract(self) -> "PlanDesignRequest":
        """Restrict the create action to creation operations."""
        invalid = [change.op for change in self.changes if change.op != "create"]
        if self.action == "create" and invalid:
            raise ValueError("create action accepts only changes with op='create'")
        return self


class TransactionDesignRequest(_RequestBase):
    """Apply, cancel, or rollback one design transaction."""

    action: Literal["apply", "cancel", "rollback"]
    transaction_id: str = Field(min_length=1)


class MetricsDesignRequest(_RequestBase):
    """Read proxy efficiency metrics without fabricating token counts."""

    action: Literal["metrics"]

    @model_validator(mode="after")
    def require_task_id(self) -> "MetricsDesignRequest":
        """Require the task whose accumulated metrics should be read."""
        if not self.task_id:
            raise ValueError("metrics action requires task_id from a prior response")
        return self


class GetEditorRequestDesignRequest(_RequestBase):
    """Read a pending or stored editor handoff request without CAD access."""

    action: Literal["get_editor_request"]
    request_id: str = Field(min_length=1, max_length=128)


class GetDraftContextDesignRequest(_RequestBase):
    """Read compact draft context and affected semantic entities for an active draft."""

    action: Literal["get_draft_context"]
    drawing_name: Optional[str] = Field(default=None, max_length=256)


class PreviewEditorRequestDesignRequest(_RequestBase):
    """Validate and preview a stored editor request as a design transaction."""

    action: Literal["preview_editor_request"]
    request_id: str = Field(min_length=1, max_length=128)


DesignRequest = Annotated[
    Union[
        InspectDesignRequest,
        GetContextDesignRequest,
        GetResultDesignRequest,
        PlanDesignRequest,
        TransactionDesignRequest,
        MetricsDesignRequest,
        GetEditorRequestDesignRequest,
        GetDraftContextDesignRequest,
        PreviewEditorRequestDesignRequest,
    ],
    Field(discriminator="action"),
]


@dataclass(frozen=True)
class PlanStep:
    """One deterministic step executed locally by the MCP server."""

    stage: Literal["inspect", "analyze", "plan", "validate", "apply", "verify"]
    description: str
    operation_count: int = 0


@dataclass(frozen=True)
class ExecutionPlan:
    """Inspectable plan that does not require agent-driven micro-operations."""

    plan_id: str
    task_id: str
    drawing_name: str
    base_revision: str
    steps: List[PlanStep]
    operation_count: int
    affected_semantic_ids: List[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        drawing_name: str,
        base_revision: str,
        operations: List[Dict[str, Any]],
    ) -> "ExecutionPlan":
        """Build the standard local stages for one deterministic change batch."""
        affected = sorted(
            {
                str(operation["semantic_id"])
                for operation in operations
                if operation.get("semantic_id")
            }
        )
        return cls(
            plan_id=f"plan_{uuid.uuid4().hex}",
            task_id=task_id,
            drawing_name=drawing_name,
            base_revision=base_revision,
            steps=[
                PlanStep("inspect", "Capture a thread-neutral CAD snapshot"),
                PlanStep("analyze", "Build or reuse the semantic topology graph"),
                PlanStep(
                    "plan",
                    "Compile approved changes into deterministic CAD operations",
                    len(operations),
                ),
                PlanStep("validate", "Validate geometry, hosts, and dependencies"),
                PlanStep(
                    "apply",
                    "Apply all operations in one CAD undo group",
                    len(operations),
                ),
                PlanStep("verify", "Re-analyze CAD and atomically update sidecars"),
            ],
            operation_count=len(operations),
            affected_semantic_ids=affected,
        )

    def compact(self) -> Dict[str, Any]:
        """Return the default compact representation."""
        return {
            "plan_id": self.plan_id,
            "operation_count": self.operation_count,
            "affected_semantic_ids": self.affected_semantic_ids,
            "stages": [step.stage for step in self.steps],
        }

    def detailed(self) -> Dict[str, Any]:
        """Return all debug-facing plan fields."""
        return asdict(self)
