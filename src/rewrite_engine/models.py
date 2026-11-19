"""Typed models for memory-only graph rewrite rules and proposals."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodePattern(BaseModel):
    """Match one semantic graph node by ontology class and field values."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    alias: str = Field(min_length=1, max_length=64)
    ontology_class: Optional[str] = Field(default=None, alias="@type")
    properties: Dict[str, Any] = Field(default_factory=dict)


class RelationPattern(BaseModel):
    """Require a JSON-LD relation between two matched node aliases."""

    model_config = ConfigDict(extra="forbid")

    source_alias: str = Field(min_length=1, max_length=64)
    predicate: str = Field(min_length=1)
    target_alias: str = Field(min_length=1, max_length=64)


class RewritePatch(BaseModel):
    """One graph edit expressed independently of CAD mutation."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["upsert_node", "patch_node", "delete_node"]
    target_alias: Optional[str] = Field(default=None, min_length=1, max_length=64)
    semantic_id: Optional[str] = Field(default=None, min_length=1)
    node: Optional[Dict[str, Any]] = None
    patch: Dict[str, Any] = Field(default_factory=dict)
    label: Optional[str] = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_patch_contract(self) -> "RewritePatch":
        """Require enough information to compile a deterministic graph edit."""
        if self.op == "upsert_node" and self.node is None:
            raise ValueError("upsert_node requires node")
        if self.op == "upsert_node" and not (
            self.semantic_id or self.target_alias or self.node.get("@id")
        ):
            raise ValueError("upsert_node requires a semantic ID")
        if self.op == "patch_node" and not self.patch:
            raise ValueError("patch_node requires patch")
        if self.op in {"patch_node", "delete_node"} and not (
            self.target_alias or self.semantic_id
        ):
            raise ValueError(f"{self.op} requires target_alias or semantic_id")
        if (
            self.semantic_id
            and self.node is not None
            and self.node.get("@id") is not None
            and self.node["@id"] != self.semantic_id
        ):
            raise ValueError("semantic_id and node @id must agree")
        return self


class GraphRewriteRule(BaseModel):
    """A Markov-style semantic graph rewrite rule."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=256)
    patterns: List[NodePattern] = Field(min_length=1, max_length=8)
    relations: List[RelationPattern] = Field(default_factory=list, max_length=16)
    rewrites: List[RewritePatch] = Field(min_length=1, max_length=32)
    max_applications: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def validate_bindings(self) -> "GraphRewriteRule":
        """Keep aliases unambiguous and patch targets resolvable."""
        aliases = [pattern.alias for pattern in self.patterns]
        if len(aliases) != len(set(aliases)):
            raise ValueError("pattern aliases must be unique")
        for relation in self.relations:
            if (
                relation.source_alias not in aliases
                or relation.target_alias not in aliases
            ):
                raise ValueError("Relation aliases must name node patterns")
        for rewrite in self.rewrites:
            if rewrite.target_alias and rewrite.target_alias not in aliases:
                raise ValueError(f"Unknown target alias '{rewrite.target_alias}'")
        return self


class GraphRewriteMatch(BaseModel):
    """One concrete binding of a rewrite rule to graph nodes."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    bindings: Dict[str, str]


class GraphRewriteProposal(BaseModel):
    """A non-mutating rewrite result ready to hand to preview code."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    description: str
    match: GraphRewriteMatch
    graph_before_revision: str
    graph_after_revision: str
    graph: Dict[str, Any]
    editor_action: Dict[str, Any]
    topology_changes: List[Dict[str, Any]] = Field(default_factory=list)
    mutated: bool = False
