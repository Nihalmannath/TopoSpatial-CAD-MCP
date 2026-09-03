import { useEffect, useRef } from "react";
import cytoscape, { type Core, type EventObject } from "cytoscape";
import type { LayoutMode } from "./types";
import { computeLayoutPositions } from "./drawingLayout";
import { OUTSIDE_SPACE_ID, type ProjectedGraph } from "./graphProjection";
import { ZONE_COLORS } from "./archUtils";

const CY_STYLE: any = [
  {
    selector: "node",
    style: {
      label: "data(label)",
      color: "#f8fafc",
      "font-family": "Inter, system-ui, sans-serif",
      "font-size": "data(fontSize)",
      "font-weight": 600,
      "text-valign": "center",
      "text-halign": "center",
      "text-wrap": "wrap",
      "text-max-width": 90,
      "background-color": "data(bgColor)",
      "border-width": "data(borderWidth)",
      "border-color": "data(borderColor)",
      "border-style": "data(borderStyle)",
      "border-opacity": 0.95,
      shape: "data(shape)",
      width: "data(width)",
      height: "data(height)",
      "transition-property": "border-color border-width background-color opacity width height",
      "transition-duration": 150,
    },
  },
  {
    selector: "node[isOutside = 1]",
    style: {
      "background-color": "#064e3b",
      "border-color": "#10b981",
      "border-style": "dashed",
      "border-width": 2.5,
      shape: "round-rectangle",
      color: "#a7f3d0",
    },
  },
  {
    selector: "node[isEntry = 1]",
    style: {
      "border-width": 3.5,
      "border-style": "double",
      "border-color": "#34d399",
      "underlay-color": "#34d399",
      "underlay-padding": 7,
      "underlay-opacity": 0.25,
    },
  },
  // Relationship: Portal Connectivity (Solid vibrant line with badge)
  {
    selector: 'edge[category = "portal_connectivity"]',
    style: {
      width: 3.5,
      "line-color": "#0284c7",
      "target-arrow-color": "#0284c7",
      "target-arrow-shape": "none",
      "curve-style": "bezier",
      label: "data(edgeLabel)",
      "font-size": 9,
      "font-family": "JetBrains Mono, monospace",
      color: "#bae6fd",
      "text-rotation": "autorotate",
      "text-background-color": "#0c4a6e",
      "text-background-opacity": 0.95,
      "text-background-padding": 3,
      "text-background-shape": "roundrectangle",
      "text-border-color": "#38bdf8",
      "text-border-width": 1,
      "text-border-opacity": 0.8,
    },
  },
  {
    selector: 'edge[category = "portal_connectivity"][isFacade = 1]',
    style: {
      "line-color": "#10b981",
      "text-background-color": "#064e3b",
      "text-border-color": "#34d399",
      color: "#6ee7b7",
      width: 4,
    },
  },
  // Relationship: Spatial Intent (Dashed purple/cyan line)
  {
    selector: 'edge[category = "spatial_intent"]',
    style: {
      width: 2.5,
      "line-style": "dashed",
      "line-dash-pattern": [6, 4],
      "line-color": "#a855f7",
      "target-arrow-color": "#a855f7",
      "target-arrow-shape": "none",
      "curve-style": "bezier",
      label: "data(edgeLabel)",
      "font-size": 8.5,
      "font-family": "Inter, sans-serif",
      color: "#e9d5ff",
      "text-rotation": "autorotate",
      "text-background-color": "#581c87",
      "text-background-opacity": 0.9,
      "text-background-padding": 2.5,
      "text-background-shape": "roundrectangle",
    },
  },
  // Relationship: Geometric Adjacency (Dotted subtle gray line)
  {
    selector: 'edge[category = "geometric_adjacency"]',
    style: {
      width: 1.5,
      "line-style": "dotted",
      "line-color": "#475569",
      "target-arrow-shape": "none",
      "curve-style": "bezier",
      label: "data(edgeLabel)",
      "font-size": 8,
      "font-family": "Inter, sans-serif",
      color: "#94a3b8",
      "text-rotation": "autorotate",
      "text-background-color": "#1e293b",
      "text-background-opacity": 0.7,
      "text-background-padding": 2,
    },
  },
  // Interactive Highlights
  {
    selector: ".selected-node",
    style: {
      "border-color": "#facc15",
      "border-width": 4,
      "underlay-color": "#facc15",
      "underlay-padding": 8,
      "underlay-opacity": 0.3,
      "z-index": 999,
      "font-weight": 700,
    },
  },
  {
    selector: ".connect-source-node",
    style: {
      "border-color": "#38bdf8",
      "border-width": 4,
      "underlay-color": "#38bdf8",
      "underlay-padding": 10,
      "underlay-opacity": 0.35,
      "z-index": 999,
    },
  },
  {
    selector: ".selected-edge",
    style: {
      "line-color": "#facc15",
      width: 5,
      "z-index": 998,
    },
  },
  {
    selector: ".egress-path",
    style: {
      "line-color": "#10b981",
      width: 5,
      "target-arrow-shape": "triangle",
      "target-arrow-color": "#10b981",
      "z-index": 997,
    },
  },
  {
    selector: ".failed",
    style: {
      "border-color": "#ef4444",
      "border-width": 3.5,
      "line-color": "#ef4444",
    },
  },
  { selector: ".dimmed", style: { opacity: 0.15 } },
  { selector: ".filtered-out", style: { display: "none" } },
];

interface UseCytoscapeGraphProps {
  containerRef: React.RefObject<HTMLDivElement>;
  projectedGraph: ProjectedGraph;
  layoutMode: LayoutMode;
  selectedId: string | null;
  connectSourceId: string | null;
  failedIds: Set<string>;
  egressEdgeIds?: Set<string>;
  onSelect: (id: string | null) => void;
  onNodeClick: (id: string) => void;
  onEdgeClick: (id: string, semanticId?: string) => void;
}

export function useCytoscapeGraph({
  containerRef,
  projectedGraph,
  layoutMode,
  selectedId,
  connectSourceId,
  failedIds,
  egressEdgeIds,
  onSelect,
  onNodeClick,
  onEdgeClick,
}: UseCytoscapeGraphProps) {
  const cyRef = useRef<Core | null>(null);
  const fittedGraphKey = useRef("");
  const layoutModeRef = useRef(layoutMode);
  layoutModeRef.current = layoutMode;
  const graphRef = useRef(projectedGraph);
  graphRef.current = projectedGraph;

  const callbacksRef = useRef({ onSelect, onNodeClick, onEdgeClick });
  callbacksRef.current = { onSelect, onNodeClick, onEdgeClick };

  // 1. Initialize Cytoscape once
  useEffect(() => {
    if (!containerRef.current) return;

    const cy = cytoscape({
      container: containerRef.current,
      boxSelectionEnabled: false,
      autounselectify: true,
      style: CY_STYLE,
    });

    cy.on("tap", "node", (evt: EventObject) => {
      const id = evt.target.id();
      callbacksRef.current.onNodeClick(id);
    });

    cy.on("tap", "edge", (evt: EventObject) => {
      const semanticId = evt.target.data("connectionId") || evt.target.data("semanticId");
      callbacksRef.current.onEdgeClick(evt.target.id(), semanticId);
    });

    cy.on("tap", (evt: EventObject) => {
      if (evt.target === cy) {
        callbacksRef.current.onSelect(null);
      }
    });

    cy.on("mouseover", "node", () => containerRef.current?.style.setProperty("cursor", "pointer"));
    cy.on("mouseout", "node", () => containerRef.current?.style.setProperty("cursor", "grab"));

    cyRef.current = cy;

    let frame = 0;
    const resizeGraph = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const container = containerRef.current;
        if (!container || container.clientWidth <= 1 || container.clientHeight <= 1) return;
        cy.resize();
        const positions = computeLayoutPositions(
          graphRef.current,
          layoutModeRef.current,
          container.clientWidth,
          container.clientHeight,
        );
        cy.batch(() => {
          graphRef.current.nodes.forEach((node) => {
            const element = cy.getElementById(node.id);
            const position = positions.get(node.id);
            if (element.length && position && !element.grabbed()) element.position(position);
          });
        });
        if (cy.elements().length > 0) cy.fit(undefined, 40);
      });
    };
    const resizeObserver = new ResizeObserver(resizeGraph);
    resizeObserver.observe(containerRef.current);
    resizeGraph();

    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      cy.destroy();
      cyRef.current = null;
      fittedGraphKey.current = "";
    };
  }, [containerRef]);

  // 2. Incremental Sync of Nodes & Edges
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    const keep = new Set<string>();
    const container = containerRef.current;
    const width = container?.clientWidth || 800;
    const height = container?.clientHeight || 600;

    const positions = computeLayoutPositions(projectedGraph, layoutMode, width, height);

    cy.batch(() => {
      // Upsert nodes
      for (const node of projectedGraph.nodes) {
        keep.add(node.id);
        const zc = ZONE_COLORS[node.zone] || ZONE_COLORS.unassigned;
        const isEntry = node.properties?.is_entry ? 1 : 0;
        const isOutside = node.isOutside ? 1 : 0;

        const areaStr = node.areaM2 > 0 ? `\n${node.areaM2.toFixed(1)} m²` : "";
        const label = `${node.label}${areaStr}`;

        const scaleFactor = node.areaM2 > 0 ? Math.min(1.5, Math.max(0.9, Math.sqrt(node.areaM2 / 14))) : 1;
        const nodeWidth = Math.round(80 * scaleFactor);
        const nodeHeight = Math.round(52 * scaleFactor);
        const fontSize = Math.round(10 * Math.min(1.2, scaleFactor));

        const nodeData = {
          id: node.id,
          label,
          isOutside,
          isEntry,
          bgColor: isOutside ? "#064e3b" : "#0f172a",
          borderColor: isOutside ? "#10b981" : zc.stroke,
          borderStyle: isOutside ? "dashed" : "solid",
          borderWidth: isOutside ? 2.5 : 2,
          shape: "round-rectangle",
          width: nodeWidth,
          height: nodeHeight,
          fontSize,
        };

        const existing = cy.getElementById(node.id);
        const pos = positions.get(node.id);

        if (existing.length === 0) {
          cy.add({
            group: "nodes",
            data: nodeData,
            position: pos ? { x: pos.x, y: pos.y } : undefined,
          });
        } else {
          existing.data(nodeData);
          if (pos && !existing.grabbed()) {
            existing.position(pos);
          }
        }
      }

      // Upsert edges
      for (const edge of projectedGraph.edges) {
        keep.add(edge.id);

        let edgeLabel = edge.label;
        if (edge.category === "portal_connectivity") {
          const icon = edge.isFaçade ? "FAÇADE 🚪" : edge.doorType === "opening" ? "↔" : "🚪";
          edgeLabel = `${icon} ${Math.round(edge.clearWidthMm || 900)}mm`;
        }

        const edgeData = {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          category: edge.category,
          edgeLabel,
          isFacade: edge.isFaçade ? 1 : 0,
          connectionId: edge.connectionId,
          doorType: edge.doorType,
          clearWidthMm: edge.clearWidthMm,
        };

        const existing = cy.getElementById(edge.id);
        if (existing.length === 0) {
          cy.add({
            group: "edges",
            data: edgeData,
          });
        } else {
          existing.data(edgeData);
        }
      }

      // Remove defunct elements
      cy.elements().forEach((el) => {
        if (!keep.has(el.id())) cy.remove(el);
      });
    });
    const graphKey = `${layoutMode}:${projectedGraph.nodes.map((node) => node.id).join("|")}`;
    if (projectedGraph.nodes.length && fittedGraphKey.current !== graphKey && width > 1 && height > 1) {
      cy.resize();
      cy.fit(undefined, 40);
      fittedGraphKey.current = graphKey;
    }
  }, [projectedGraph, layoutMode, containerRef]);

  // 3. Selection, Egress, and Diagnostic Highlights
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    cy.batch(() => {
      cy.elements().removeClass("selected-node selected-edge connect-source-node failed dimmed egress-path");

      if (connectSourceId) {
        cy.getElementById(connectSourceId).addClass("connect-source-node");
      }

      if (selectedId) {
        const element = cy.getElementById(selectedId);
        const edges = cy.edges(`[connectionId = "${selectedId}"]`);
        if (element.length > 0) {
          element.addClass(element.isNode() ? "selected-node" : "selected-edge");
        }
        if (edges.length > 0) {
          edges.addClass("selected-edge");
        }
        const neighborhood = element.union(edges).closedNeighborhood();
        if (neighborhood.length > 0) {
          cy.elements().not(neighborhood).addClass("dimmed");
        }
      }

      if (egressEdgeIds && egressEdgeIds.size > 0) {
        egressEdgeIds.forEach((id) => {
          cy.getElementById(id).addClass("egress-path");
          cy.edges(`[connectionId = "${id}"]`).addClass("egress-path");
        });
      }

      failedIds.forEach((id) => {
        cy.getElementById(id).addClass("failed");
        cy.edges(`[connectionId = "${id}"]`).addClass("failed");
      });
    });
  }, [selectedId, connectSourceId, failedIds, egressEdgeIds]);

  return { cyRef };
}
