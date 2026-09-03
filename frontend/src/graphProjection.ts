import type {
  GraphDocument,
  GraphNode,
  Point,
  RelationshipCategory,
  RoomZone,
} from "./types";
import { computeCentroid, computePolygonAreaM2, inferRoomZone } from "./archUtils";

export const OUTSIDE_SPACE_ID = "urn:topospatial:space:outside";

export interface ProjectedNodeData {
  id: string;
  label: string;
  type: string;
  isOutside: boolean;
  isRoom: boolean;
  zone: RoomZone;
  areaM2: number;
  cadX: number;
  cadY: number;
  boundary: Point[];
  handles: string[];
  properties: Record<string, any>;
  hasSpatialProgram: boolean;
  connectedCount: number;
  diagnosticCount: number;
}

export interface ProjectedEdgeData {
  id: string;
  source: string;
  target: string;
  category: RelationshipCategory;
  label: string;
  connectionId?: string;
  viaDoorId?: string;
  clearWidthMm?: number;
  doorType?: string;
  isFaçade: boolean;
  status?: string;
  priority?: string;
  intentKind?: string;
  isDirected: boolean;
  properties: Record<string, any>;
}

export interface ProjectedGraph {
  nodes: ProjectedNodeData[];
  edges: ProjectedEdgeData[];
  nodesById: Map<string, ProjectedNodeData>;
  edgesById: Map<string, ProjectedEdgeData>;
  cadBounds: { minX: number; minY: number; maxX: number; maxY: number };
}

export function projectGraph(graphDoc: GraphDocument | undefined): ProjectedGraph {
  const nodes = graphDoc?.["@graph"] || [];
  const nodesById = new Map<string, ProjectedNodeData>();
  const edgesById = new Map<string, ProjectedEdgeData>();
  const projectedNodes: ProjectedNodeData[] = [];
  const projectedEdges: ProjectedEdgeData[] = [];

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  // 1. First pass: extract all Space / Room nodes and Outside anchor
  for (const node of nodes) {
    const type = node["@type"];
    const id = node["@id"];
    const isOutside = id === OUTSIDE_SPACE_ID;
    const isRoom = type === "top:Room" || type === "top:Space" || isOutside;

    if (isRoom) {
      const boundary = (node["cad:geometry"]?.boundary as Point[]) || [];
      const centroid = computeCentroid(boundary);
      const label = node["rdfs:label"] || (isOutside ? "Exterior Façade" : id.split(":").pop() || "Space");
      const areaFromProp = node["cad:areaSquareMetres"] || node["top:hasArea"];
      const areaM2 = areaFromProp && areaFromProp > 0 ? areaFromProp : computePolygonAreaM2(boundary);
      const zone = isOutside ? "outdoor" : inferRoomZone(label, node["cad:properties"]);
      const handles = node["cad:handles"] || [];
      const hasSpatialProgram = Boolean(node["cad:properties"]?.spatial_program);

      let cadX = centroid[0];
      let cadY = centroid[1];

      if (boundary.length > 0) {
        for (const [x, y] of boundary) {
          if (x < minX) minX = x;
          if (y < minY) minY = y;
          if (x > maxX) maxX = x;
          if (y > maxY) maxY = y;
        }
      }

      const pNode: ProjectedNodeData = {
        id,
        label,
        type,
        isOutside,
        isRoom: true,
        zone,
        areaM2,
        cadX,
        cadY,
        boundary,
        handles,
        properties: node["cad:properties"] || {},
        hasSpatialProgram,
        connectedCount: 0,
        diagnosticCount: 0,
      };

      nodesById.set(id, pNode);
      projectedNodes.push(pNode);
    }
  }

  // Adjust outside anchor position if unbounded
  const outsideReferenced = nodes.some((node) => {
    if (node["@type"] !== "top:Connection") return false;
    const geometry = node["cad:geometry"] || {};
    return geometry.from_space_id === OUTSIDE_SPACE_ID || geometry.to_space_id === OUTSIDE_SPACE_ID;
  });
  if (outsideReferenced && !nodesById.has(OUTSIDE_SPACE_ID)) {
    const outside: ProjectedNodeData = {
      id: OUTSIDE_SPACE_ID,
      label: "Exterior Façade",
      type: "top:Space",
      isOutside: true,
      isRoom: true,
      zone: "outdoor",
      areaM2: 0,
      cadX: isFinite(minX) && isFinite(maxX) ? minX - Math.max(maxX - minX, 1000) * 0.25 : -100,
      cadY: isFinite(minY) && isFinite(maxY) ? (minY + maxY) / 2 : 0,
      boundary: [],
      handles: [],
      properties: { space_type: "outdoor", anchor: "exterior" },
      hasSpatialProgram: false,
      connectedCount: 0,
      diagnosticCount: 0,
    };
    nodesById.set(outside.id, outside);
    projectedNodes.push(outside);
  }

  if (isFinite(minX) && isFinite(maxX)) {
    const outsideNode = nodesById.get(OUTSIDE_SPACE_ID);
    if (outsideNode && outsideNode.boundary.length === 0) {
      outsideNode.cadX = minX - (maxX - minX) * 0.25;
      outsideNode.cadY = (minY + maxY) / 2;
    }
  } else {
    minX = 0;
    minY = 0;
    maxX = 10000;
    maxY = 10000;
  }

  // 2. Second pass: extract connections, spatial intents, and adjacencies
  for (const node of nodes) {
    const type = node["@type"];
    const id = node["@id"];

    if (type === "top:Connection") {
      const geom = node["cad:geometry"] || {};
      const from = geom.from_space_id;
      const to = geom.to_space_id;
      if (!from || !to) continue;

      const isFaçade = from === OUTSIDE_SPACE_ID || to === OUTSIDE_SPACE_ID;
      const width = geom.clear_width_mm || 900;
      const doorType = geom.door_type || "door";
      const status = geom.status || "confirmed";
      const isCandidate = status === "candidate";

      const edgeId = id;
      const pEdge: ProjectedEdgeData = {
        id: edgeId,
        source: from,
        target: to,
        category: isCandidate ? "geometric_adjacency" : "portal_connectivity",
        label: isCandidate ? "Adjacent" : `${doorType === "opening" ? "Opening" : "Door"} (${Math.round(width)}mm)`,
        connectionId: id,
        viaDoorId: geom.via_id || geom.via_door_id,
        clearWidthMm: width,
        doorType,
        isFaçade,
        status,
        isDirected: false,
        properties: node["cad:properties"] || {},
      };

      edgesById.set(edgeId, pEdge);
      projectedEdges.push(pEdge);

      const srcNode = nodesById.get(from);
      const tgtNode = nodesById.get(to);
      if (srcNode) srcNode.connectedCount++;
      if (tgtNode) tgtNode.connectedCount++;
    } else if (type === "top:SpatialIntent") {
      const geom = node["cad:geometry"] || {};
      const props = node["cad:properties"] || {};
      const from = geom.from_space_id || props.source_space_id || props.source;
      const to = geom.to_space_id || props.target_space_id || props.target;
      if (!from || !to) continue;

      const kind = props.kind || geom.kind || "adjacent";
      const priority = props.priority || geom.priority || "should";
      const direction = props.direction || geom.direction || "bidirectional";

      const edgeId = id;
      const pEdge: ProjectedEdgeData = {
        id: edgeId,
        source: from,
        target: to,
        category: "spatial_intent",
        label: `${kind} (${priority})`,
        status: "active",
        priority,
        intentKind: kind,
        isDirected: direction !== "bidirectional",
        isFaçade: false,
        properties: props,
      };

      edgesById.set(edgeId, pEdge);
      projectedEdges.push(pEdge);
    }
  }

  // Derived geometric adjacency is a separate fact from portal connectivity.
  const adjacencyKeys = new Set<string>();
  for (const node of nodes) {
    if (node["@type"] !== "top:Room" && node["@type"] !== "top:Space") continue;
    for (const target of node["top:adjacentTo"] || []) {
      const targetId = target?.["@id"];
      if (!targetId || !nodesById.has(targetId)) continue;
      const [left, right] = [node["@id"], targetId].sort();
      const key = `${left}|${right}`;
      if (adjacencyKeys.has(key)) continue;
      adjacencyKeys.add(key);
      const edgeId = `adjacency:${left}:${right}`;
      const edge: ProjectedEdgeData = {
        id: edgeId,
        source: left,
        target: right,
        category: "geometric_adjacency",
        label: "Adjacent",
        isFaçade: false,
        isDirected: false,
        properties: {},
      };
      edgesById.set(edgeId, edge);
      projectedEdges.push(edge);
    }
  }

  return {
    nodes: projectedNodes,
    edges: projectedEdges,
    nodesById,
    edgesById,
    cadBounds: { minX, minY, maxX, maxY },
  };
}
