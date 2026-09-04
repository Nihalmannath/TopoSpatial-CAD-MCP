"""Local orchestration of high-level architectural design transactions."""

from __future__ import annotations

import copy
import logging
import time
import uuid
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from core.config import get_config
from core.exceptions import CADBusyError
from topology_engine import ChangeDocument, TopologyEngine, TransactionStore
from topology_engine.cad_bridge import CADTopologyBridge
from topology_engine.wall_network import centerline_key, wall_spec_key

from .cache import AnalysisCache, ResultStore
from .models import (
    ContextBudget,
    DesignRequest,
    ExecutionPlan,
    GetContextDesignRequest,
    GetResultDesignRequest,
    InspectDesignRequest,
    MetricsDesignRequest,
    PlanDesignRequest,
    TransactionDesignRequest,
)
from .observability import MetricsStore
from .retry import FailureMemory, RetryPolicy

logger = logging.getLogger(__name__)


class DesignOrchestrator:
    """Turn approved architectural intent into safe local execution plans."""

    def __init__(
        self,
        *,
        engine: TopologyEngine | None = None,
        bridge: CADTopologyBridge | None = None,
        transactions: TransactionStore | None = None,
        cache: AnalysisCache | None = None,
        metrics: MetricsStore | None = None,
    ) -> None:
        """Initialize reusable topology, transaction, cache, and metrics services."""
        topology = get_config().topology
        self.engine = engine or TopologyEngine(
            snap_tolerance_mm=topology.snap_tolerance_mm,
            max_opening_gap_mm=topology.max_opening_gap_mm,
            min_room_dimension_mm=topology.min_room_dimension_mm,
        )
        self.bridge = bridge or CADTopologyBridge()
        self.transactions = transactions or TransactionStore(
            ttl_seconds=topology.transaction_ttl_seconds
        )
        self.cache = cache or AnalysisCache()
        self.results = ResultStore(ttl_seconds=topology.transaction_ttl_seconds)
        self.metrics = metrics or MetricsStore()
        self.retry = RetryPolicy()
        self.failures = FailureMemory()
        self._analysis_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="topospatial-design"
        )

    def execute(self, request: DesignRequest, adapter: Any | None) -> Dict[str, Any]:
        """Execute one typed high-level action and return a compact dictionary."""
        started = time.perf_counter()
        req_task_id = getattr(request, "task_id", None)
        task_id = req_task_id or f"task_{uuid.uuid4().hex}"
        request_data = (
            request.model_dump(mode="json", by_alias=True)
            if hasattr(request, "model_dump")
            else dict(request)
        )
        request_data["task_id"] = req_task_id or "__anonymous__"
        request_key = self.failures.request_key(request_data)
        self.metrics.add(task_id, mcp_calls=1)

        blocked = self.failures.blocked(request_key)
        if blocked is not None:
            fingerprint, repeat_count = blocked
            result = {
                "success": False,
                "task_id": task_id,
                "error": {
                    "code": "REPEATED_FAILURE_STOPPED",
                    "stage": request.action,
                    "retryable": False,
                    "details": "The same request already failed twice.",
                    "suggested_action": (
                        "Change the request or inspect the prior error before retrying."
                    ),
                    "fingerprint": fingerprint,
                    "repeat_count": repeat_count,
                },
            }
            self._record_elapsed(task_id, started)
            return result

        try:
            if isinstance(request, MetricsDesignRequest):
                result = {
                    "success": True,
                    "task_id": task_id,
                    "metrics": self.metrics.snapshot(task_id),
                    "token_usage": None,
                    "token_usage_note": (
                        "Model token usage is not observable by this MCP server."
                    ),
                    "cache": self.cache.stats(),
                }
            elif isinstance(request, GetResultDesignRequest):
                result = self._get_result(request, task_id)
            else:
                if adapter is None:
                    raise RuntimeError("A CAD connection is required for this action")
                result = self._dispatch(request, adapter, task_id)
            self.failures.clear(request_key)
            self._record_elapsed(task_id, started)
            return result
        except Exception as exc:
            logger.exception("Design action '%s' failed", request.action)
            self.metrics.add(task_id, failures=1)
            result = self._failure_response(
                task_id=task_id,
                action=request.action,
                request_key=request_key,
                exc=exc,
            )
            self._record_elapsed(task_id, started)
            return result

    def record_response_size(self, task_id: str, byte_count: int) -> None:
        """Add serialized response size after the MCP tool formats the result."""
        self.metrics.add(task_id, response_bytes=int(byte_count))

    def _dispatch(
        self, request: DesignRequest, adapter: Any, task_id: str
    ) -> Dict[str, Any]:
        action = getattr(request, "action", "")
        if isinstance(request, InspectDesignRequest) or action == "inspect":
            return self._inspect(request, adapter, task_id)
        if isinstance(request, GetContextDesignRequest) or action == "get_context":
            return self._get_context(request, adapter, task_id)
        if isinstance(request, PlanDesignRequest) or action in {"create", "modify", "validate", "preview"}:
            return self._plan(request, adapter, task_id)
        if isinstance(request, TransactionDesignRequest) or action in {"apply", "cancel", "rollback"}:
            if action == "apply":
                return self._apply(request, adapter, task_id)
            if action == "cancel":
                return self._cancel(request, task_id)
            return self._rollback(request, adapter, task_id)
        raise ValueError(f"Unsupported design action '{action}'")

    def _get_result(
        self, request: GetResultDesignRequest, task_id: str
    ) -> Dict[str, Any]:
        stored = self.results.get(request.result_id)
        nodes = stored.get("nodes", [])
        end = request.offset + request.max_entities
        return {
            "success": True,
            "task_id": task_id,
            "result_id": request.result_id,
            "drawing": stored.get("drawing"),
            "revision": stored.get("revision"),
            "root": stored.get("root"),
            "offset": request.offset,
            "count": len(nodes[request.offset : end]),
            "nodes": nodes[request.offset : end],
            "next_offset": end if end < len(nodes) else None,
            "truncated": end < len(nodes),
        }

    def _snapshot_and_analysis(
        self,
        adapter: Any,
        scope: str,
        task_id: str,
        expected_drawing: Optional[str] = None,
    ) -> Tuple[Any, Dict[str, Any], bool]:
        if expected_drawing and hasattr(adapter, "document") and adapter.document:
            try:
                if adapter.document.Name != expected_drawing and hasattr(adapter, "switch_drawing"):
                    adapter.switch_drawing(expected_drawing)
            except Exception:
                pass
        snapshot, retries = self._run_cad(
            adapter, lambda: self.bridge.snapshot(adapter, scope)
        )
        if retries:
            self.metrics.add(task_id, retries=retries)
        self.metrics.add(task_id, entities_inspected=len(snapshot.entities))
        cached = self.cache.get(snapshot)
        if cached is not None:
            _, analysis = cached
            return snapshot, analysis, True

        def analyze() -> Dict[str, Any]:
            if any(
                entity.semantic.get("ontology_class") == "top:Room"
                for entity in snapshot.entities
            ):
                self.engine.ensure_available()
            return self.engine.analyze(snapshot)

        analysis = self._analysis_executor.submit(analyze).result(timeout=120)
        self.cache.put(snapshot, analysis)
        self.metrics.add(task_id, topology_operations=1)
        return snapshot, analysis, False

    def _inspect(
        self, request: InspectDesignRequest, adapter: Any, task_id: str
    ) -> Dict[str, Any]:
        snapshot, analysis, cache_hit = self._snapshot_and_analysis(
            adapter, request.scope, task_id
        )
        nodes = analysis.get("graph", {}).get("@graph", [])
        classes = Counter(node.get("@type", "unknown") for node in nodes)
        issues = self._classify_analysis_issues(analysis.get("issues", []))
        result: Dict[str, Any] = {
            "success": True,
            "task_id": task_id,
            "drawing": snapshot.drawing_name,
            "revision": snapshot.revision,
            "units": snapshot.units,
            "counts": {
                "cad_entities": len(snapshot.entities),
                "semantic_nodes": len(nodes),
                "relations": analysis.get("relation_count", 0),
                "room_candidates": analysis.get("candidate_count", 0),
                "by_class": dict(sorted(classes.items())),
            },
            "problems": self._problem_summary(issues),
            "needs_llm_decision": any(
                item["classification"] == "NEEDS_LLM_DECISION" for item in issues
            ),
            "cache_hit": cache_hit,
        }
        if request.detail_level in {"normal", "detailed", "debug"}:
            result["issues"] = issues[: request.budget.max_entities]
            result["candidates"] = [
                self._compact_candidate(candidate, request.budget.include_geometry)
                for candidate in analysis.get("candidates", [])[
                    : request.budget.max_entities
                ]
            ]
        if request.detail_level in {"detailed", "debug"}:
            result["semantic_index"] = [
                self._compact_node(node, request.budget)
                for node in nodes[: request.budget.max_entities]
            ]
        if request.detail_level == "debug":
            result["cache"] = self.cache.stats()
        return result

    def _get_context(
        self, request: GetContextDesignRequest, adapter: Any, task_id: str
    ) -> Dict[str, Any]:
        snapshot, analysis, cache_hit = self._snapshot_and_analysis(
            adapter, request.scope, task_id
        )
        nodes = analysis.get("graph", {}).get("@graph", [])
        nodes_by_id = {str(node.get("@id")): node for node in nodes}
        start_id = self._resolve_entity(request.entity, nodes)
        links = self._semantic_links(nodes)
        ordered_ids, hard_truncated = self._bounded_walk(
            start_id,
            links,
            depth=request.budget.graph_depth,
            max_neighbors=request.budget.max_neighbors,
            max_entities=500,
        )
        all_context_nodes = [
            self._compact_node(nodes_by_id[node_id], request.budget)
            for node_id in ordered_ids
            if node_id in nodes_by_id
        ]
        context_nodes = all_context_nodes[: request.budget.max_entities]
        truncated = hard_truncated or len(all_context_nodes) > len(context_nodes)
        result: Dict[str, Any] = {
            "success": True,
            "task_id": task_id,
            "drawing": snapshot.drawing_name,
            "revision": snapshot.revision,
            "root": start_id,
            "depth": request.budget.graph_depth,
            "count": len(context_nodes),
            "nodes": context_nodes,
            "truncated": truncated,
            "cache_hit": cache_hit,
        }
        if truncated:
            result["result_id"] = self.results.put(
                {
                    "drawing": snapshot.drawing_name,
                    "revision": snapshot.revision,
                    "root": start_id,
                    "nodes": all_context_nodes,
                }
            )
            result["suggested_action"] = (
                "Increase budget.max_entities or narrow graph_depth only if more "
                "context is required."
            )
        return result

    def _plan(
        self,
        request: PlanDesignRequest,
        adapter: Any,
        task_id: str,
    ) -> Dict[str, Any]:
        snapshot, analysis, cache_hit = self._snapshot_and_analysis(
            adapter, "all", task_id
        )
        if snapshot.units != "mm":
            raise ValueError(
                "Design changes require an AutoCAD drawing with INSUNITS=mm"
            )
        ordered_changes = self._order_changes(request.changes)
        dependency_order_normalized = ordered_changes != list(request.changes)
        document = ChangeDocument.model_validate(
            {
                "@context": {"top": "http://w3id.org/topologicpy#"},
                "base_revision": request.base_revision,
                "changes": [
                    change.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for change in ordered_changes
                ],
            }
        )
        self._analysis_executor.submit(self.engine.ensure_available).result(timeout=120)
        operations, diff, warnings = self.engine.plan_changes(
            document, snapshot, analysis
        )
        self.bridge.resolve_operation_representations(adapter, operations, analysis)
        for item, operation in zip(diff, operations):
            item["representation"] = operation.get("representation")

        problems = self._validate_execution_plan(operations, analysis)
        if dependency_order_normalized:
            problems.insert(
                0,
                self._problem(
                    "DEPENDENCY_ORDER_NORMALIZED",
                    "AUTO_FIXABLE",
                    "Hosts and parent objects were ordered before dependent openings.",
                ),
            )
        blocking = [
            problem
            for problem in problems
            if problem["classification"] in {"NEEDS_LLM_DECISION", "FATAL"}
        ]
        if blocking:
            return {
                "success": False,
                "task_id": task_id,
                "error": {
                    "code": "DESIGN_VALIDATION_FAILED",
                    "stage": "validate",
                    "retryable": False,
                    "details": (
                        "The proposed design requires correction or an architectural "
                        "decision."
                    ),
                    "suggested_action": (
                        "Revise only the listed conflicting changes and preview again."
                    ),
                },
                "problems": problems,
                "needs_llm_decision": any(
                    problem["classification"] == "NEEDS_LLM_DECISION"
                    for problem in problems
                ),
            }

        plan = ExecutionPlan.build(
            task_id=task_id,
            drawing_name=snapshot.drawing_name,
            base_revision=snapshot.revision,
            operations=operations,
        )
        changed = Counter(
            str(item.get("ontology_class", "unknown")).removeprefix("top:").lower()
            for item in diff
        )
        result: Dict[str, Any] = {
            "success": True,
            "task_id": task_id,
            "drawing": snapshot.drawing_name,
            "base_revision": snapshot.revision,
            "plan_id": plan.plan_id,
            "changed": dict(sorted(changed.items())),
            "operation_count": len(operations),
            "warnings": warnings,
            "problems": self._problem_summary(problems),
            "needs_llm_decision": False,
            "mutated": False,
            "cache_hit": cache_hit,
        }
        if request.action == "validate":
            result["validated"] = True
        else:
            transaction = self.transactions.create(
                drawing_name=snapshot.drawing_name,
                base_revision=snapshot.revision,
                operations=operations,
                diff=diff,
                warnings=warnings,
                metadata={"task_id": task_id, "execution_plan": plan.detailed()},
            )
            result.update(
                {
                    "transaction_id": transaction.transaction_id,
                    "expires_at": transaction.expires_at,
                }
            )
        if request.detail_level in {"normal", "detailed", "debug"}:
            result["execution_plan"] = plan.compact()
            result["affected"] = plan.affected_semantic_ids
        if request.detail_level in {"detailed", "debug"}:
            result["diff"] = diff
            result["execution_plan"] = plan.detailed()
        if request.detail_level == "debug":
            result["operations"] = operations
        return result

    def _apply(
        self, request: TransactionDesignRequest, adapter: Any, task_id: str
    ) -> Dict[str, Any]:
        transaction = self.transactions.get(request.transaction_id)
        if transaction.status == "cancelled":
            raise ValueError("The preview transaction was cancelled")
        if transaction.status == "rolled_back":
            return {
                "success": True,
                "task_id": task_id,
                "transaction_id": request.transaction_id,
                "idempotent": True,
                "status": "rolled_back",
            }
        if transaction.status == "applied":
            return {
                "success": True,
                "task_id": task_id,
                "transaction_id": request.transaction_id,
                "idempotent": True,
                "status": "applied",
                "result": self._compact_apply_result(
                    transaction.result or {}, request.detail_level
                ),
            }

        current, _, _ = self._snapshot_and_analysis(adapter, "all", task_id)
        if current.drawing_name != transaction.drawing_name:
            raise ValueError("The active drawing changed after preview")
        if current.revision != transaction.base_revision:
            raise ValueError("The drawing changed after preview; preview again")

        cad_result, retries = self._run_cad(
            adapter,
            lambda: self.bridge.apply_operations(
                adapter,
                transaction.operations,
                refresh=False,
                rollback_revision=current.revision,
            ),
        )
        self.metrics.add(
            task_id,
            retries=retries,
            cad_operations=len(transaction.operations),
            entities_modified=len(cad_result.get("modified_handles", []))
            + sum(
                len(item.get("handles", [])) for item in cad_result.get("created", [])
            ),
        )
        try:
            updated, retries = self._run_cad(
                adapter, lambda: self.bridge.snapshot(adapter, scope="all")
            )
            self.metrics.add(
                task_id, retries=retries, entities_inspected=len(updated.entities)
            )
            self.cache.invalidate(current.drawing_name)
            analysis = self._analysis_executor.submit(
                lambda: self.engine.analyze(updated)
            ).result(timeout=120)
            self.metrics.add(task_id, topology_operations=1)
            self.cache.put(updated, analysis)
            turtle = self.engine.to_turtle(analysis["graph"])
            sidecars = self.bridge.write_sidecars(updated, analysis["graph"], turtle)
        except Exception:
            logger.exception("Post-apply verification failed; rolling CAD back")
            self.bridge.rollback_last_transaction(
                adapter, expected_revision=current.revision
            )
            self.cache.invalidate(current.drawing_name)
            adapter.refresh_view()
            raise

        result = {
            "drawing": updated.drawing_name,
            "base_revision": current.revision,
            "revision": updated.revision,
            "cad": cad_result,
            "sidecars": sidecars,
            "node_count": analysis["node_count"],
            "relation_count": analysis["relation_count"],
            "operation_count": len(transaction.operations),
        }
        self.transactions.mark_applied(request.transaction_id, result)
        adapter.refresh_view()
        return {
            "success": True,
            "task_id": task_id,
            "transaction_id": request.transaction_id,
            "status": "applied",
            "result": self._compact_apply_result(result, request.detail_level),
        }

    def _cancel(
        self, request: TransactionDesignRequest, task_id: str
    ) -> Dict[str, Any]:
        transaction = self.transactions.cancel(request.transaction_id)
        return {
            "success": True,
            "task_id": task_id,
            "transaction_id": transaction.transaction_id,
            "status": transaction.status,
            "mutated": False,
        }

    def _rollback(
        self, request: TransactionDesignRequest, adapter: Any, task_id: str
    ) -> Dict[str, Any]:
        transaction = self.transactions.get(request.transaction_id)
        if transaction.status == "rolled_back":
            return {
                "success": True,
                "task_id": task_id,
                "transaction_id": request.transaction_id,
                "status": "rolled_back",
                "idempotent": True,
            }
        if transaction.status != "applied" or not transaction.result:
            raise ValueError("Only an applied transaction can be rolled back")
        current, retries = self._run_cad(
            adapter, lambda: self.bridge.snapshot(adapter, scope="all")
        )
        self.metrics.add(
            task_id, retries=retries, entities_inspected=len(current.entities)
        )
        applied_revision = transaction.result.get("revision")
        if current.drawing_name != transaction.drawing_name:
            raise ValueError("The active drawing differs from the transaction drawing")
        if current.revision != applied_revision:
            raise ValueError(
                "Rollback is unsafe because the drawing changed after this transaction"
            )
        self.bridge.rollback_last_transaction(
            adapter, expected_revision=transaction.base_revision
        )
        restored = self.bridge.snapshot(adapter, scope="all")
        self.cache.invalidate(restored.drawing_name)
        analysis = self._analysis_executor.submit(
            lambda: self.engine.analyze(restored)
        ).result(timeout=120)
        self.metrics.add(task_id, cad_operations=1, topology_operations=1)
        self.cache.put(restored, analysis)
        sidecars = self.bridge.write_sidecars(
            restored, analysis["graph"], self.engine.to_turtle(analysis["graph"])
        )
        result = {
            "drawing": restored.drawing_name,
            "revision": restored.revision,
            "sidecars": sidecars,
        }
        self.transactions.mark_rolled_back(request.transaction_id, result)
        adapter.refresh_view()
        return {
            "success": True,
            "task_id": task_id,
            "transaction_id": request.transaction_id,
            "status": "rolled_back",
            "result": result,
        }

    def _validate_execution_plan(
        self, operations: Sequence[Dict[str, Any]], analysis: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        problems: List[Dict[str, Any]] = []
        existing_ids = {
            str(node.get("@id")) for node in analysis.get("graph", {}).get("@graph", [])
        }
        created_ids: Counter[str] = Counter()
        wall_signatures: Dict[Tuple[Any, ...], str] = {}
        wall_centerlines: Dict[Tuple[Any, ...], Tuple[str, Tuple[Any, ...]]] = {}
        room_shapes: List[Tuple[str, Any]] = []

        for node in analysis.get("graph", {}).get("@graph", []):
            if node.get("@type") != "top:Wall":
                continue
            try:
                line = tuple(centerline_key(node.get("cad:geometry", {})))
                spec = wall_spec_key(node.get("cad:geometry", {})) + (
                    str(node.get("cad:representation", "standard")),
                )
            except (TypeError, ValueError):
                continue
            wall_centerlines[line] = (str(node.get("@id")), spec)

        for operation in operations:
            semantic_id = str(operation.get("semantic_id", ""))
            kind = operation.get("kind")
            ontology_class = operation.get("ontology_class")
            if kind in {"create_managed", "create_room_boundary"} and semantic_id:
                created_ids[semantic_id] += 1
                if semantic_id in existing_ids:
                    problems.append(
                        self._problem(
                            "SEMANTIC_ID_EXISTS",
                            "FATAL",
                            f"Semantic ID '{semantic_id}' already exists.",
                            [semantic_id],
                        )
                    )
            geometry = operation.get("geometry", {})
            if kind == "create_managed" and ontology_class == "top:Wall":
                representation = str(operation.get("representation", "standard"))
                signature = self._wall_signature(geometry, representation)
                line = self._wall_centerline_signature(geometry)
                spec = wall_spec_key(geometry) + (representation,)
                prior = wall_centerlines.get(line)
                if prior is not None and prior[1] != spec:
                    problems.append(
                        self._problem(
                            "WALL_SPEC_CONFLICT",
                            "NEEDS_LLM_DECISION",
                            (
                                f"Walls '{prior[0]}' and '{semantic_id}' share a "
                                "centerline but have incompatible thickness, height, "
                                "style, or representation specifications."
                            ),
                            [prior[0], semantic_id],
                        )
                    )
                elif signature in wall_signatures or prior is not None:
                    duplicate_id = wall_signatures.get(signature) or (
                        prior[0] if prior is not None else semantic_id
                    )
                    problems.append(
                        self._problem(
                            "DUPLICATE_WALL",
                            "NEEDS_LLM_DECISION",
                            (
                                f"Walls '{duplicate_id}' and "
                                f"'{semantic_id}' have identical centerlines."
                            ),
                            [duplicate_id, semantic_id],
                        )
                    )
                else:
                    wall_signatures[signature] = semantic_id
                    wall_centerlines[line] = (semantic_id, spec)
            if kind == "create_managed" and ontology_class == "top:Room":
                shape = self._room_shape(geometry)
                if shape is not None:
                    room_shapes.append((semantic_id, shape))

        for semantic_id, count in created_ids.items():
            if count > 1:
                problems.append(
                    self._problem(
                        "DUPLICATE_SEMANTIC_ID",
                        "FATAL",
                        f"Semantic ID '{semantic_id}' is created {count} times.",
                        [semantic_id],
                    )
                )

        for index, (left_id, left_shape) in enumerate(room_shapes):
            for right_id, right_shape in room_shapes[index + 1 :]:
                intersection = left_shape.intersection(right_shape)
                if not intersection.is_empty and intersection.area > 1.0:
                    problems.append(
                        self._problem(
                            "ROOMS_OVERLAP",
                            "NEEDS_LLM_DECISION",
                            (
                                f"Rooms '{left_id}' and '{right_id}' overlap by "
                                f"{intersection.area:.1f} mm²."
                            ),
                            [left_id, right_id],
                        )
                    )
        return problems

    @staticmethod
    def _order_changes(changes: Sequence[Any]) -> List[Any]:
        """Order deterministic dependencies without changing design intent."""
        class_priority = {
            "top:Room": 0,
            "top:Wall": 0,
            "top:Door": 1,
            "top:Window": 1,
        }
        operation_priority = {
            "annotate": 0,
            "create": 1,
            "update": 2,
            "delete": 3,
        }
        return sorted(
            changes,
            key=lambda change: (
                operation_priority.get(change.op, 9),
                class_priority.get(change.ontology_class, 2),
            ),
        )

    @staticmethod
    def _wall_signature(
        geometry: Dict[str, Any], representation: str = "standard"
    ) -> Tuple[Any, ...]:
        return (
            tuple(centerline_key(geometry))
            + wall_spec_key(geometry)
            + (representation,)
        )

    @staticmethod
    def _wall_centerline_signature(geometry: Dict[str, Any]) -> Tuple[Any, ...]:
        return tuple(centerline_key(geometry))

    @staticmethod
    def _room_shape(geometry: Dict[str, Any]) -> Any | None:
        try:
            from shapely.affinity import rotate, translate
            from shapely.geometry import box

            polygon = box(
                0.0,
                0.0,
                float(geometry["clear_width"]),
                float(geometry["clear_depth"]),
            )
            polygon = rotate(
                polygon,
                float(geometry.get("rotation_deg", 0.0)),
                origin=(0.0, 0.0),
                use_radians=False,
            )
            return translate(
                polygon,
                xoff=float(geometry["origin"][0]),
                yoff=float(geometry["origin"][1]),
            )
        except (ImportError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _problem(
        code: str,
        classification: str,
        message: str,
        affected: Iterable[str] = (),
    ) -> Dict[str, Any]:
        return {
            "code": code,
            "classification": classification,
            "message": message,
            "affected": list(affected),
        }

    def _classify_analysis_issues(
        self, issues: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        classification = {
            "unsupported_geometry": "NEEDS_LLM_DECISION",
            "unsupported_ontology_class": "NEEDS_LLM_DECISION",
            "ambiguous_semantic_group": "FATAL",
            "topologic_face_failed": "FATAL",
        }
        return [
            {
                **copy.deepcopy(issue),
                "classification": classification.get(
                    str(issue.get("code")), "NEEDS_LLM_DECISION"
                ),
            }
            for issue in issues
        ]

    @staticmethod
    def _problem_summary(problems: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        counts = Counter(
            problem.get("classification", "UNKNOWN") for problem in problems
        )
        return {
            "auto_fixable": counts.get("AUTO_FIXABLE", 0),
            "needs_llm_decision": counts.get("NEEDS_LLM_DECISION", 0),
            "fatal": counts.get("FATAL", 0),
        }

    @staticmethod
    def _compact_candidate(
        candidate: Dict[str, Any], include_geometry: bool
    ) -> Dict[str, Any]:
        result = {
            "candidate_id": candidate.get("candidate_id"),
            "clear_area_m2": candidate.get("clear_area_m2"),
            "centroid": candidate.get("centroid"),
            "requires_explicit_tag": candidate.get("requires_explicit_tag", True),
        }
        if include_geometry:
            result["boundary"] = candidate.get("boundary")
        return result

    @staticmethod
    def _compact_node(node: Dict[str, Any], budget: ContextBudget) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "id": node.get("@id"),
            "class": node.get("@type"),
        }
        if node.get("rdfs:label"):
            result["label"] = node["rdfs:label"]
        relationships: Dict[str, Any] = {}
        for key, value in node.items():
            if key.startswith("top:") and isinstance(value, list):
                relationships[key] = [
                    item.get("@id")
                    for item in value[: budget.max_neighbors]
                    if isinstance(item, dict) and item.get("@id")
                ]
            elif key in {
                "cad:hostWall",
                "cad:parent",
                "cad:boundingRooms",
                "cad:aliases",
            }:
                relationships[key] = value
        if relationships:
            result["relationships"] = relationships
        if budget.include_metadata:
            result["managed"] = bool(node.get("cad:managed", False))
            result["handles"] = node.get("cad:handles", [])
            result["layers"] = node.get("cad:layers", [])
            if node.get("cad:representation"):
                result["representation"] = node["cad:representation"]
        if budget.include_geometry:
            result["geometry"] = node.get("cad:geometry", {})
        return result

    @staticmethod
    def _resolve_entity(entity: str, nodes: Sequence[Dict[str, Any]]) -> str:
        if any(node.get("@id") == entity for node in nodes):
            return entity
        alias_matches = [
            str(node.get("@id"))
            for node in nodes
            if entity in node.get("cad:aliases", [])
        ]
        if len(alias_matches) == 1:
            return alias_matches[0]
        if len(alias_matches) > 1:
            raise ValueError(
                f"Wall alias '{entity}' resolves to multiple semantic IDs: "
                f"{', '.join(sorted(alias_matches))}"
            )
        wanted = entity.strip().casefold()
        matches = [
            str(node.get("@id"))
            for node in nodes
            if str(node.get("rdfs:label", "")).strip().casefold() == wanted
        ]
        if not matches:
            raise ValueError(
                f"No semantic entity matches '{entity}'. Use inspect to obtain IDs "
                "and labels."
            )
        if len(matches) > 1:
            raise ValueError(
                f"Label '{entity}' is ambiguous; use one of these semantic IDs: "
                f"{', '.join(matches)}"
            )
        return matches[0]

    @staticmethod
    def _semantic_links(nodes: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
        links: Dict[str, set[str]] = defaultdict(set)
        known = {str(node.get("@id")) for node in nodes}
        for node in nodes:
            source = str(node.get("@id"))
            for key, value in node.items():
                targets: List[str] = []
                if key.startswith("top:") and isinstance(value, list):
                    targets = [
                        str(item["@id"])
                        for item in value
                        if isinstance(item, dict) and item.get("@id")
                    ]
                elif key in {"cad:hostWall", "cad:parent"} and isinstance(value, str):
                    targets = [value]
                for target in targets:
                    if target in known:
                        links[source].add(target)
                        links[target].add(source)
        return {key: sorted(value) for key, value in links.items()}

    @staticmethod
    def _bounded_walk(
        start_id: str,
        links: Dict[str, List[str]],
        *,
        depth: int,
        max_neighbors: int,
        max_entities: int,
    ) -> Tuple[List[str], bool]:
        queue = deque([(start_id, 0)])
        seen = {start_id}
        ordered: List[str] = []
        truncated = False
        while queue:
            node_id, level = queue.popleft()
            if len(ordered) >= max_entities:
                truncated = True
                break
            ordered.append(node_id)
            if level >= depth:
                continue
            neighbors = links.get(node_id, [])
            if len(neighbors) > max_neighbors:
                truncated = True
            for neighbor in neighbors[:max_neighbors]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append((neighbor, level + 1))
        return ordered, truncated or bool(queue)

    @staticmethod
    def _compact_apply_result(
        result: Dict[str, Any], detail_level: str
    ) -> Dict[str, Any]:
        compact = {
            "drawing": result.get("drawing"),
            "revision": result.get("revision"),
            "operation_count": result.get("operation_count", 0),
            "node_count": result.get("node_count", 0),
            "relation_count": result.get("relation_count", 0),
        }
        if detail_level in {"normal", "detailed", "debug"}:
            compact["sidecars"] = result.get("sidecars", {})
        if detail_level in {"detailed", "debug"}:
            compact["cad"] = result.get("cad", {})
        return compact

    def _failure_response(
        self,
        *,
        task_id: str,
        action: str,
        request_key: str,
        exc: Exception,
    ) -> Dict[str, Any]:
        details = str(exc) or exc.__class__.__name__
        folded = details.casefold()
        code = "DESIGN_OPERATION_FAILED"
        stage = action
        retryable = False
        suggested = "Correct the request using the reported details."
        if "revision" in folded or "changed after preview" in folded:
            code = "DRAWING_REVISION_STALE"
            stage = "preview" if action != "apply" else "apply"
            suggested = "Inspect the drawing again and create a new preview."
        elif "transaction_id" in folded or "transaction" in folded:
            code = "TRANSACTION_INVALID"
            suggested = "Create a fresh preview and use its transaction_id."
        elif "insunits" in folded:
            code = "DRAWING_UNITS_INVALID"
            stage = "validate"
            suggested = "Set AutoCAD INSUNITS to millimetres and preview again."
        elif isinstance(exc, CADBusyError) or "cad_busy" in folded:
            code = "CAD_BUSY"
            stage = action
            retryable = False
            suggested = (
                "AutoCAD is busy, running a command, or displaying a modal dialog. "
                "Dismiss any open dialogs in AutoCAD and try again."
            )
        elif any(marker in folded for marker in RetryPolicy.TRANSIENT_MARKERS):
            code = "CAD_TRANSIENT_FAILURE"
            retryable = True
            suggested = "Ensure AutoCAD is responsive, then retry once."
        elif "no semantic entity" in folded or "ambiguous" in folded:
            code = "SEMANTIC_ENTITY_UNRESOLVED"
            stage = "query"
            suggested = "Use inspect or an exact semantic ID."
        fingerprint = self.failures.error_fingerprint(code, stage, details)
        repeat_count = self.failures.record(request_key, fingerprint)
        return {
            "success": False,
            "task_id": task_id,
            "error": {
                "code": code,
                "stage": stage,
                "retryable": retryable and repeat_count < 2,
                "details": details,
                "suggested_action": suggested,
                "fingerprint": fingerprint,
                "repeat_count": repeat_count,
            },
        }

    def _record_elapsed(self, task_id: str, started: float) -> None:
        self.metrics.add(
            task_id, execution_time_ms=(time.perf_counter() - started) * 1000.0
        )

    def _run_cad(
        self, adapter: Any, operation: Any, allow_reconnect: bool = False
    ) -> Tuple[Any, int]:
        """Run a CAD operation serialized on COM worker without mid-transaction reconnect."""
        from adapters.com_worker import run_com

        def execute_on_worker():
            return operation()

        def reconnect(_exc: Exception) -> None:
            if not allow_reconnect:
                return
            connect = getattr(adapter, "connect", None)
            if callable(connect):
                connect(only_if_running=True, allow_launch=False)

        cad_type = getattr(adapter, "cad_type", "autocad")
        on_retry = reconnect if allow_reconnect else None
        return self.retry.run(
            lambda: run_com(execute_on_worker, cad_type=cad_type),
            on_retry=on_retry,
        )
