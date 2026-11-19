"""Memory-only graph rewrite execution."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List

from design_engine.workspace import TopologyWorkspaceService
from topology_engine.navigation import graph_revision

from .compiler import RewriteCompiler
from .matcher import GraphMatcher
from .models import GraphRewriteProposal, GraphRewriteRule


class RewriteEngine:
    """Apply Markov-inspired rewrite rules to semantic graph copies only."""

    def __init__(
        self,
        matcher: GraphMatcher | None = None,
        compiler: RewriteCompiler | None = None,
    ) -> None:
        """Use existing editor command behavior on detached graph copies."""
        self.matcher = matcher or GraphMatcher()
        self.compiler = compiler or RewriteCompiler()
        self._workspace = TopologyWorkspaceService()

    def propose(
        self, graph: Dict[str, Any], rules: Iterable[GraphRewriteRule]
    ) -> List[GraphRewriteProposal]:
        """Return preview-compatible proposals without mutating the input graph."""
        before_revision = str(graph.get("cad:graphRevision") or graph_revision(graph))
        proposals: List[GraphRewriteProposal] = []
        for rule in rules:
            for match in self.matcher.find_matches(graph, rule):
                action = self.compiler.compile_editor_action(rule, match, graph)
                proposal_graph = copy.deepcopy(graph)
                for command in action.commands:
                    self._workspace._apply_command(proposal_graph, command)
                proposal_graph["@graph"] = sorted(
                    proposal_graph.get("@graph", []), key=lambda item: item["@id"]
                )
                proposal_graph["cad:graphRevision"] = graph_revision(proposal_graph)
                proposals.append(
                    GraphRewriteProposal(
                        rule_id=rule.rule_id,
                        description=rule.description,
                        match=match,
                        graph_before_revision=before_revision,
                        graph_after_revision=proposal_graph["cad:graphRevision"],
                        graph=proposal_graph,
                        editor_action=action.model_dump(mode="json"),
                        topology_changes=action.topology_changes,
                        mutated=False,
                    )
                )
        return proposals
