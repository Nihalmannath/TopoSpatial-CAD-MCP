"""Deterministic semantic graph pattern matching."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .models import GraphRewriteMatch, GraphRewriteRule, NodePattern


class GraphMatcher:
    """Find simple node-pattern bindings in a JSON-LD semantic graph."""

    def find_matches(
        self, graph: Dict[str, Any], rule: GraphRewriteRule
    ) -> List[GraphRewriteMatch]:
        """Return deterministic matches for a rule without mutating the graph."""
        nodes = sorted(
            graph.get("@graph", []), key=lambda item: str(item.get("@id", ""))
        )
        candidates = [
            [node for node in nodes if self._matches_pattern(node, pattern)]
            for pattern in rule.patterns
        ]
        matches: List[GraphRewriteMatch] = []
        nodes_by_id = {str(node.get("@id")): node for node in nodes}
        self._bind(rule, candidates, nodes_by_id, 0, {}, set(), matches)
        return matches[: rule.max_applications]

    def _bind(
        self,
        rule: GraphRewriteRule,
        candidates: List[List[Dict[str, Any]]],
        nodes_by_id: Dict[str, Dict[str, Any]],
        index: int,
        bindings: Dict[str, str],
        used_ids: set[str],
        matches: List[GraphRewriteMatch],
    ) -> None:
        if len(matches) >= rule.max_applications:
            return
        if index >= len(rule.patterns):
            if self._relations_match(rule, bindings, nodes_by_id):
                matches.append(
                    GraphRewriteMatch(rule_id=rule.rule_id, bindings=dict(bindings))
                )
            return
        pattern = rule.patterns[index]
        for node in candidates[index]:
            node_id = str(node.get("@id", ""))
            if not node_id or node_id in used_ids:
                continue
            bindings[pattern.alias] = node_id
            used_ids.add(node_id)
            self._bind(
                rule, candidates, nodes_by_id, index + 1, bindings, used_ids, matches
            )
            used_ids.remove(node_id)
            bindings.pop(pattern.alias, None)

    @staticmethod
    def _matches_pattern(node: Dict[str, Any], pattern: NodePattern) -> bool:
        if pattern.ontology_class and node.get("@type") != pattern.ontology_class:
            return False
        return all(
            _read_path(node, key.split(".")) == value
            for key, value in pattern.properties.items()
        )

    @staticmethod
    def _relations_match(
        rule: GraphRewriteRule,
        bindings: Dict[str, str],
        nodes_by_id: Dict[str, Dict[str, Any]],
    ) -> bool:
        for relation in rule.relations:
            source = nodes_by_id[bindings[relation.source_alias]]
            links = source.get(relation.predicate, [])
            if not isinstance(links, list) or not any(
                isinstance(link, dict)
                and link.get("@id") == bindings[relation.target_alias]
                for link in links
            ):
                return False
        return True


def _read_path(value: Dict[str, Any], path: Iterable[str]) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
