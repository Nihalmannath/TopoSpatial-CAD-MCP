import type { LayoutMode } from "./types";
import type { ProjectedGraph, ProjectedNodeData } from "./graphProjection";

export interface NodePosition {
  x: number;
  y: number;
}

/**
 * Compute positions for all nodes based on the chosen LayoutMode.
 * In floor_plan mode: coordinates are mapped from real CAD world coordinates (inverted Y for standard canvas).
 * In connectivity_map mode: coordinates are computed or arranged to optimize relationship visibility.
 */
export function computeLayoutPositions(
  graph: ProjectedGraph,
  mode: LayoutMode,
  viewportWidth: number = 800,
  viewportHeight: number = 600,
): Map<string, NodePosition> {
  const positions = new Map<string, NodePosition>();
  const nodes = graph.nodes;

  if (nodes.length === 0) return positions;

  if (mode === "floor_plan") {
    // True CAD coordinates normalized to viewport center
    const { minX, minY, maxX, maxY } = graph.cadBounds;
    const spanX = Math.max(maxX - minX, 1000);
    const spanY = Math.max(maxY - minY, 1000);

    // Scale so drawing fits in roughly 70% of viewport
    const targetSpan = Math.min(viewportWidth, viewportHeight) * 0.7;
    const scale = targetSpan / Math.max(spanX, spanY);

    const centerX = (minX + maxX) / 2;
    const centerY = (minY + maxY) / 2;

    for (const node of nodes) {
      // In CAD +Y is up; in canvas / cytoscape +Y is down
      const x = (node.cadX - centerX) * scale;
      const y = -(node.cadY - centerY) * scale; // Invert Y
      positions.set(node.id, { x, y });
    }
  } else {
    // Abstract Connectivity Map: topological bubble layout
    // Arrange in an intelligent concentric or spring circle cluster
    const count = nodes.length;
    const radius = Math.min(viewportWidth, viewportHeight) * 0.35;
    const angleStep = (2 * Math.PI) / Math.max(count, 1);

    // Sort nodes so connected rooms are clustered
    const sorted = [...nodes].sort((a, b) => {
      // Entry / outside first
      if (a.isOutside) return -1;
      if (b.isOutside) return 1;
      if (a.zone === "circulation" && b.zone !== "circulation") return -1;
      if (b.zone === "circulation" && a.zone !== "circulation") return 1;
      return b.connectedCount - a.connectedCount;
    });

    sorted.forEach((node, index) => {
      if (node.isOutside) {
        // Position outside node to the left
        positions.set(node.id, { x: -radius * 1.3, y: 0 });
      } else {
        const angle = index * angleStep - Math.PI / 2;
        // Jiggle radius slightly based on zone for organic grouping
        let r = radius;
        if (node.zone === "circulation") r *= 0.55;
        const x = Math.cos(angle) * r;
        const y = Math.sin(angle) * r;
        positions.set(node.id, { x, y });
      }
    });
  }

  return positions;
}
