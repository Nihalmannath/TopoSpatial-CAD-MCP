import { useCallback, useMemo, useRef, useState } from "react";
import type {
  Direction,
  Discovery,
  EditorAction,
  GraphNode,
  GraphTool,
  IntentKind,
  IntentPriority,
  LayoutMode,
  StudioMode,
} from "./types";
import { projectGraph, type ProjectedEdgeData, type ProjectedNodeData } from "./graphProjection";
import { useCytoscapeGraph } from "./useCytoscapeGraph";
import { displayName, spatialIntentId } from "./spatial";
import { DOOR_PRESETS } from "./archUtils";

interface Props {
  nodes: GraphNode[];
  selectedId: string | null;
  failedIds: Set<string>;
  mode?: StudioMode;
  onSelect: (id: string | null) => void;
  onCommitAction?: (action: EditorAction) => Promise<void> | void;
  discoveries?: Discovery[];
}

interface FilterState {
  showPortals: boolean;
  showIntents: boolean;
  showAdjacencies: boolean;
}

const newActionId = () => crypto.randomUUID();

async function digestId(prefix: string, value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  const hex = Array.from(new Uint8Array(bytes))
    .map((n) => n.toString(16).padStart(2, "0"))
    .join("");
  return `${prefix}${hex.slice(0, 20)}`;
}

async function connectionId(left: string, right: string, via = "") {
  const [a, b] = [left, right].sort();
  return digestId("urn:topospatial:connection:", `${a}|${b}|${via || "logical"}`);
}

export function GraphView({
  nodes,
  selectedId,
  failedIds,
  mode = "current_drawing",
  onSelect,
  onCommitAction,
  discoveries = [],
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  const [layoutMode, setLayoutMode] = useState<LayoutMode>("floor_plan");
  const [activeTool, setActiveTool] = useState<GraphTool>("select");
  const [connectSourceId, setConnectSourceId] = useState<string | null>(null);
  const [intentKind, setIntentKind] = useState<IntentKind>("adjacent");
  const [intentPriority, setIntentPriority] = useState<IntentPriority>("should");
  const [doorPresetIndex, setDoorPresetIndex] = useState(1); // 900mm standard
  const [egressStartId, setEgressStartId] = useState<string | null>(null);

  const [filters, setFilters] = useState<FilterState>({
    showPortals: true,
    showIntents: true,
    showAdjacencies: true,
  });

  const isDesignStudy = mode === "design_study";

  // Build projected graph data
  const graphDoc = useMemo(() => ({ "@graph": nodes, "cad:revision": "", "cad:graphRevision": "" }), [nodes]);
  const projected = useMemo(() => projectGraph(graphDoc), [graphDoc]);

  // Compute egress edge path if egress tool is active
  const egressEdgeIds = useMemo(() => {
    if (!egressStartId) return new Set<string>();
    // Simple BFS to find shortest path to outside or entry room
    const queue: Array<{ curr: string; path: string[] }> = [{ curr: egressStartId, path: [] }];
    const visited = new Set<string>([egressStartId]);

    while (queue.length > 0) {
      const { curr, path } = queue.shift()!;
      const currNode = projected.nodesById.get(curr);
      if (currNode?.isOutside || currNode?.properties?.is_entry) {
        return new Set(path);
      }

      // Find adjacent portal edges
      for (const edge of projected.edges) {
        if (edge.category !== "portal_connectivity") continue;
        const next = edge.source === curr ? edge.target : edge.target === curr ? edge.source : null;
        if (next && !visited.has(next)) {
          visited.add(next);
          queue.push({ curr: next, path: [...path, edge.id] });
        }
      }
    }
    return new Set<string>();
  }, [egressStartId, projected]);

  // Handle node interaction
  const handleNodeClick = useCallback(
    async (nodeId: string) => {
      if (activeTool === "select") {
        onSelect(nodeId);
        return;
      }

      if (activeTool === "inspect") {
        onSelect(nodeId);
        return;
      }

      if (activeTool === "door" && isDesignStudy) {
        if (!connectSourceId) {
          setConnectSourceId(nodeId);
          onSelect(nodeId);
        } else if (connectSourceId === nodeId) {
          setConnectSourceId(null);
        } else {
          // Connect connectSourceId -> nodeId via doorway
          if (onCommitAction) {
            const srcNode = nodes.find((n) => n["@id"] === connectSourceId);
            const tgtNode = nodes.find((n) => n["@id"] === nodeId);
            const srcName = displayName(srcNode);
            const tgtName = displayName(tgtNode);
            const preset = DOOR_PRESETS[doorPresetIndex] || DOOR_PRESETS[1];
            const cid = await connectionId(connectSourceId, nodeId, "door");

            const newNode: GraphNode = {
              "@id": cid,
              "@type": "top:Connection",
              "rdfs:label": `${srcName} to ${tgtName} (${preset.width}mm)`,
              "cad:managed": true,
              "cad:geometry": {
                from_space_id: connectSourceId,
                to_space_id: nodeId,
                clear_width_mm: preset.width,
                door_type: preset.type,
                direction: "bidirectional" as Direction,
                accessible: preset.width >= 900,
                status: "confirmed",
                source: "explicit",
              },
            };

            await onCommitAction({
              action_id: newActionId(),
              description: `Add doorway: ${srcName} ↔ ${tgtName} (${preset.width}mm)`,
              commands: [{ op: "upsert_node", semantic_id: cid, node: newNode }],
              topology_changes: [],
            });
            onSelect(cid);
          }
          setConnectSourceId(null);
          setActiveTool("select");
        }
        return;
      }

      if (activeTool === "intent" && isDesignStudy) {
        if (!connectSourceId) {
          setConnectSourceId(nodeId);
          onSelect(nodeId);
        } else if (connectSourceId === nodeId) {
          setConnectSourceId(null);
        } else {
          // Add spatial intent between connectSourceId and nodeId
          if (onCommitAction) {
            const srcNode = nodes.find((n) => n["@id"] === connectSourceId);
            const tgtNode = nodes.find((n) => n["@id"] === nodeId);
            const srcName = displayName(srcNode);
            const tgtName = displayName(tgtNode);
            const iid = await spatialIntentId(connectSourceId, nodeId, intentKind, "bidirectional");

            const newNode: GraphNode = {
              "@id": iid,
              "@type": "top:SpatialIntent",
              "rdfs:label": `${intentPriority} be ${intentKind.replace("_", " ")}: ${srcName} & ${tgtName}`,
              "cad:managed": true,
              "cad:properties": {
                source_space_id: connectSourceId,
                target_space_id: nodeId,
                kind: intentKind,
                priority: intentPriority,
                direction: "bidirectional",
                evaluation_status: "unverified",
              },
            };

            await onCommitAction({
              action_id: newActionId(),
              description: `Define intent: ${srcName} ${intentKind.replace("_", " ")} ${tgtName}`,
              commands: [{ op: "upsert_node", semantic_id: iid, node: newNode }],
              topology_changes: [],
            });
            onSelect(iid);
          }
          setConnectSourceId(null);
          setActiveTool("select");
        }
        return;
      }
    },
    [
      activeTool,
      connectSourceId,
      doorPresetIndex,
      intentKind,
      intentPriority,
      isDesignStudy,
      nodes,
      onCommitAction,
      onSelect,
    ]
  );

  const handleEdgeClick = useCallback(
    (_edgeId: string, semanticId?: string) => {
      if (semanticId) {
        onSelect(semanticId);
      }
    },
    [onSelect]
  );

  const { cyRef } = useCytoscapeGraph({
    containerRef,
    projectedGraph: projected,
    layoutMode,
    selectedId,
    connectSourceId,
    failedIds,
    egressEdgeIds,
    onSelect,
    onNodeClick: handleNodeClick,
    onEdgeClick: handleEdgeClick,
  });

  // Zoom and fit controls
  const fitView = useCallback(() => cyRef.current?.fit(undefined, 40), [cyRef]);
  const zoomIn = useCallback(() => {
    const cy = cyRef.current;
    if (cy) cy.zoom({ level: Math.min(cy.zoom() * 1.3, 4), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }, [cyRef]);
  const zoomOut = useCallback(() => {
    const cy = cyRef.current;
    if (cy) cy.zoom({ level: Math.max(cy.zoom() / 1.3, 0.2), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }, [cyRef]);

  // Selected element lookup
  const selectedNode = useMemo(() => nodes.find((n) => n["@id"] === selectedId), [nodes, selectedId]);
  const selectedProjectedNode: ProjectedNodeData | undefined = selectedId ? projected.nodesById.get(selectedId) : undefined;
  const selectedProjectedEdge: ProjectedEdgeData | undefined = selectedId
    ? projected.edges.find((e) => e.connectionId === selectedId || e.id === selectedId)
    : undefined;

  // Selected action handlers (for Design Study mode)
  const deleteSelected = async () => {
    if (!onCommitAction || !selectedId || !selectedNode) return;
    const name = displayName(selectedNode);
    await onCommitAction({
      action_id: newActionId(),
      description: `Delete ${selectedNode["@type"].replace("top:", "")}: ${name}`,
      commands: [{ op: "delete_node", semantic_id: selectedId }],
      topology_changes: [{ op: "delete", "@id": selectedId }],
    });
    onSelect(null);
  };

  const toggleDirection = async () => {
    if (!onCommitAction || !selectedNode) return;
    const geom = (selectedNode["cad:geometry"] || {}) as Record<string, unknown>;
    const currentDir = (geom.direction as Direction) || "bidirectional";
    const nextDir: Direction = currentDir === "bidirectional" ? "forward" : currentDir === "forward" ? "reverse" : "bidirectional";
    await onCommitAction({
      action_id: newActionId(),
      description: `Change portal direction to ${nextDir}`,
      commands: [{ op: "patch_node", semantic_id: selectedNode["@id"], patch: { "cad:geometry": { ...geom, direction: nextDir } } }],
      topology_changes: [],
    });
  };

  return (
    <div className="graph-view architect-graph-view">
      <div className="graph-health-strip" aria-label="Graph health">
        <span><strong>{projected.nodes.length}</strong> spaces</span>
        <span><strong>{projected.edges.filter((edge) => edge.category === "portal_connectivity").length}</strong> doors/openings</span>
        <span><strong>{discoveries.filter((item) => item.kind !== "space").length}</strong> portal discoveries</span>
        <span className={discoveries.some((item) => item.status === "ambiguous" || item.reason) ? "health-warning" : ""}>
          <strong>{discoveries.filter((item) => item.status === "ambiguous" || item.reason).length}</strong> unresolved
        </span>
      </div>
      {/* Primary Toolbar: Tools + Modes */}
      <div className="graph-edit-toolbar">
        <div className="tool-group">
          <button
            type="button"
            className={`graph-tool-btn ${activeTool === "select" ? "active" : ""}`}
            onClick={() => {
              setActiveTool("select");
              setConnectSourceId(null);
              setEgressStartId(null);
            }}
            title="Inspect rooms, doors, and relationships"
          >
            <span className="tool-icon">↖</span> Inspect
          </button>

          <button
            type="button"
            className={`graph-tool-btn ${activeTool === "door" ? "active" : ""}`}
            disabled={!isDesignStudy}
            onClick={() => {
              setActiveTool("door");
              setConnectSourceId(null);
              setEgressStartId(null);
            }}
            title={isDesignStudy ? "Insert doorway or portal between two spaces" : "Switch to Design Study to add doors"}
          >
            <span className="tool-icon">🚪</span> Add Doorway
          </button>

          <button
            type="button"
            className={`graph-tool-btn ${activeTool === "intent" ? "active" : ""}`}
            disabled={!isDesignStudy}
            onClick={() => {
              setActiveTool("intent");
              setConnectSourceId(null);
              setEgressStartId(null);
            }}
            title={isDesignStudy ? "Define spatial adjacency or privacy intent" : "Switch to Design Study to add intent"}
          >
            <span className="tool-icon">⚯</span> Add Intent
          </button>
        </div>

        {/* Secondary contextual tool options */}
        {activeTool === "door" && isDesignStudy && (
          <div className="intent-tool-options">
            <select
              className="intent-mini-select"
              value={doorPresetIndex}
              onChange={(e) => setDoorPresetIndex(Number(e.target.value))}
            >
              {DOOR_PRESETS.map((preset, idx) => (
                <option key={preset.label} value={idx}>
                  {preset.label}
                </option>
              ))}
            </select>
          </div>
        )}

        {activeTool === "intent" && isDesignStudy && (
          <div className="intent-tool-options">
            <select
              className="intent-mini-select"
              value={intentKind}
              onChange={(e) => setIntentKind(e.target.value as IntentKind)}
            >
              <option value="adjacent">Adjacent</option>
              <option value="direct_access">Direct Access</option>
              <option value="near">Near</option>
              <option value="separated">Separated</option>
              <option value="visual_connection">Visual View</option>
              <option value="acoustic_separation">Acoustic Guard</option>
            </select>
            <select
              className="intent-mini-select"
              value={intentPriority}
              onChange={(e) => setIntentPriority(e.target.value as IntentPriority)}
            >
              <option value="must">Must</option>
              <option value="should">Should</option>
              <option value="prefer">Prefer</option>
              <option value="avoid">Avoid</option>
              <option value="must_not">Must Not</option>
            </select>
          </div>
        )}

        {connectSourceId && (
          <div className="connect-mode-hint">
            <span>
              Linking from <strong>{displayName(nodes.find((n) => n["@id"] === connectSourceId))}</strong> — click destination room
            </span>
            <button type="button" className="cancel-mini-btn" onClick={() => setConnectSourceId(null)}>
              ✕
            </button>
          </div>
        )}

        {/* Layout & Zoom Controls */}
        <div className="graph-layout-controls">
          <div className="layout-toggle-pills">
            <button
              type="button"
              className={`layout-pill ${layoutMode === "floor_plan" ? "active" : ""}`}
              onClick={() => setLayoutMode("floor_plan")}
              title="True CAD plan coordinates and spatial orientation"
            >
              Floor-plan Topology
            </button>
            <button
              type="button"
              className={`layout-pill ${layoutMode === "connectivity_map" ? "active" : ""}`}
              onClick={() => setLayoutMode("connectivity_map")}
              title="Abstract topological bubble diagram of circulation"
            >
              Connectivity Map
            </button>
          </div>

          <button type="button" className="graph-tool" title="Zoom In" onClick={zoomIn}>
            +
          </button>
          <button type="button" className="graph-tool" title="Zoom Out" onClick={zoomOut}>
            −
          </button>
          <button type="button" className="graph-tool" title="Fit to Screen" onClick={fitView}>
            ⤢
          </button>
        </div>
      </div>

      {/* Canvas */}
      <div ref={containerRef} className="graph-canvas" />

      {/* In-Graph Selected Context Card */}
      {(selectedProjectedNode || selectedProjectedEdge || selectedNode) && (
        <div className="graph-selection-card">
          <div className="card-header">
            <div className="card-title">
              <span className="type-badge">
                {selectedProjectedEdge
                  ? selectedProjectedEdge.category.replace("_", " ")
                  : selectedNode?.["@type"].replace("top:", "") || "Space"}
              </span>
              <strong>
                {selectedProjectedNode?.label ||
                  selectedProjectedEdge?.label ||
                  (selectedNode ? displayName(selectedNode) : "Element")}
              </strong>
            </div>
            <button type="button" className="close-mini-btn" onClick={() => onSelect(null)}>
              ✕
            </button>
          </div>

          <div className="card-body">
            {selectedProjectedNode && (
              <div className="space-metrics">
                <div className="meta-line">
                  <span>Zone: <strong>{selectedProjectedNode.zone}</strong></span>
                  <span>Area: <strong>{selectedProjectedNode.areaM2.toFixed(1)} m²</strong></span>
                </div>
                {selectedProjectedNode.isOutside && (
                  <p className="hint-text">Exterior Building Façade Anchor</p>
                )}
              </div>
            )}

            {selectedProjectedEdge && (
              <div className="connection-metrics">
                <div className="meta-line">
                  <span>Category: <strong>{selectedProjectedEdge.category.replace("_", " ")}</strong></span>
                  {selectedProjectedEdge.clearWidthMm && (
                    <span>Width: <strong>{Math.round(selectedProjectedEdge.clearWidthMm)}mm</strong></span>
                  )}
                </div>
                {selectedProjectedEdge.isFaçade && (
                  <p className="hint-text success">Verified Exterior Façade Exit</p>
                )}
              </div>
            )}

            {isDesignStudy && (
              <div className="btn-row">
                {selectedNode?.["@type"] === "top:Connection" && (
                  <button type="button" className="quick-btn" onClick={toggleDirection}>
                    ⇄ Direction
                  </button>
                )}
                <button type="button" className="quick-btn delete" onClick={deleteSelected}>
                  🗑 Delete
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Legend & Summary */}
      <div className="graph-legend">
        <span className="legend-item">
          <i className="lg-line portal" /> Door / Opening ({projected.edges.filter((e) => e.category === "portal_connectivity").length})
        </span>
        <span className="legend-item">
          <i className="lg-line intent" /> Spatial Intent ({projected.edges.filter((e) => e.category === "spatial_intent").length})
        </span>
        <span className="legend-item">
          <i className="lg-line adjacency" /> Geometric Adjacency
        </span>
        <span className="legend-count">
          {projected.nodes.length} Spaces · {projected.edges.length} Relationships
        </span>
      </div>
    </div>
  );
}
