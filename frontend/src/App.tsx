import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { applyPreview, cancelPreview, getDraft, getHealth, getWorkspace, openSession, previewDraft, rebaseDraft, resetDraft, route, storeDraft, subscribeEvents } from "./api";
import { DrawingDiscoveriesPanel } from "./DrawingDiscoveriesPanel";
import { GraphView } from "./GraphView";
import { McpHandoffPanel } from "./McpHandoffPanel";
import { PlanCanvas } from "./PlanCanvas";
import { RelationshipInspector } from "./RelationshipInspector";
import { SpaceInspector } from "./SpaceInspector";
import { SpaceProgramPanel } from "./SpaceProgramPanel";
import { displayName } from "./spatial";
import type {
  Direction,
  Discovery,
  DraftResponse,
  EditorAction,
  GraphNode,
  PreviewResponse,
  RoomZone,
  SavedDraft,
  ServerEvent,
  SpaceType,
  StudioMode,
  TopologyChange,
  WorkspaceResponse,
} from "./types";

const SPACE_TYPES = new Set(["top:Room", "top:Space"]);
const PORTAL_TYPES = new Set(["top:Door", "top:Opening"]);
const newActionId = () => crypto.randomUUID();

async function digestId(prefix: string, value: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  const hex = Array.from(new Uint8Array(bytes)).map((n) => n.toString(16).padStart(2, "0")).join("");
  return `${prefix}${hex.slice(0, 20)}`;
}

async function connectionId(left: string, right: string, via = "") {
  const [a, b] = [left, right].sort();
  return digestId("urn:topospatial:connection:", `${a}|${b}|${via || "logical"}`);
}

const flattenCommands = (actions: EditorAction[], position: number) => actions.slice(0, position).flatMap((action) => action.commands);
const flattenChanges = (actions: EditorAction[], position: number) => actions.slice(0, position).flatMap((action) => action.topology_changes);

export default function App() {
  const [mode, setMode] = useState<StudioMode>("current_drawing");
  const [base, setBase] = useState<WorkspaceResponse | null>(null);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [savedDraft, setSavedDraft] = useState<SavedDraft | null>(null);
  const [actions, setActions] = useState<EditorAction[]>([]);
  const [undoPosition, setUndoPosition] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [pendingSelection, setPendingSelection] = useState<string | null | undefined>(undefined);
  const [localFormDirty, setLocalFormDirty] = useState(false);
  const [ignoredDiscoveryIds, setIgnoredDiscoveryIds] = useState<Set<string>>(new Set());
  const inspectorSaveRef = useRef<(() => Promise<void>) | null>(null);
  const [compactView, setCompactView] = useState<"plan" | "graph" | "details">("plan");
  const [message, setMessage] = useState("Connecting to CAD engine…");
  const [syncStatus, setSyncStatus] = useState<"connected" | "connecting" | "disconnected">("connecting");
  const [backendMismatch, setBackendMismatch] = useState<string | null>(null);
  const [lastCadEvent, setLastCadEvent] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [requiredWidth, setRequiredWidth] = useState(1200);
  const [advancedGeometry, setAdvancedGeometry] = useState(false);
  const [connection, setConnection] = useState({ from: "", to: "", direction: "bidirectional" as Direction, width: 1200, accessible: true, portal: "", wall: "", physical: false });
  const [routeEnds, setRouteEnds] = useState(["", ""]);
  const [routeResult, setRouteResult] = useState<Record<string, unknown> | null>(null);
  const modeRef = useRef(mode);
  modeRef.current = mode;

  const refresh = useCallback(async (pin = false) => {
    try {
      await openSession();
      const [next, health] = await Promise.all([
        getWorkspace(modeRef.current === "design_study" && !pin ? base?.drawing : undefined),
        getHealth(),
      ]);
      const activeMcp = next.active_mcp || health.active_mcp;
      let mismatch: string | null = null;
      if (health.build_id !== next.backend.build_id) {
        mismatch = "Dashboard responses came from different backend builds. Restart TopoSpatial and AutoCAD.";
      } else if (activeMcp && activeMcp.build_id !== next.backend.build_id) {
        mismatch = "Studio and the active MCP are running different builds. Restart TopoSpatial and AutoCAD.";
      } else if (activeMcp?.drawing && activeMcp.drawing !== next.drawing) {
        mismatch = `MCP last inspected ${activeMcp.drawing}, but Studio is showing ${next.drawing}. Inspect the intended drawing in MCP to verify synchronization.`;
      } else if (activeMcp && activeMcp.drawing === next.drawing && (activeMcp.drawing_revision !== next.drawing_revision || activeMcp.graph_revision !== next.graph_revision)) {
        mismatch = "Studio and the active MCP report different drawing or graph revisions. Refresh MCP analysis after confirming the active DWG.";
      }
      setBackendMismatch(mismatch);
      const baseMoved = Boolean(base && (base.drawing_revision !== next.drawing_revision || base.graph_revision !== next.graph_revision));
      setBase(next);
      setSavedDraft((await getDraft(next.drawing)).draft);
      if (modeRef.current === "current_drawing" || pin) {
        setDraft(null);
      } else if (baseMoved) {
        setDraft((current) => current ? { ...current, frozen: true } : current);
      }
      const verified = Boolean(activeMcp?.graph_revision && !mismatch && activeMcp.drawing === next.drawing);
      setMessage(`Connected · ${next.cad_entity_count} CAD · ${next.graph["@graph"].length} semantic · MCP ${verified ? "verified" : "unverified"} · rev ${next.drawing_revision.replace("sha256:", "").slice(0, 8)}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
      setDraft((current) => modeRef.current === "design_study" && current ? { ...current, frozen: true } : current);
    }
  }, [base?.drawing, base?.drawing_revision, base?.graph_revision]);

  useEffect(() => { void refresh(true); }, []);
  useEffect(() => subscribeEvents((event: ServerEvent) => {
    const at = new Date().toLocaleTimeString();
    if (event.event_type === "cad.changed") {
      setLastCadEvent(`CAD changed at ${at}`);
      if (modeRef.current === "current_drawing") void refresh(false);
      else { setDraft((current) => current ? { ...current, frozen: true } : current); setMessage("CAD changed — Design Study frozen until Rebase or Reset"); }
    } else if (event.event_type === "workspace.applied") { setLastCadEvent(`Applied at ${at}`); void refresh(true); }
  }, setSyncStatus), [refresh]);

  useEffect(() => {
    const webview = (window as unknown as { chrome?: { webview?: { addEventListener?: (name: string, listener: (event: MessageEvent) => void) => void; removeEventListener?: (name: string, listener: (event: MessageEvent) => void) => void } } }).chrome?.webview;
    if (!webview?.addEventListener) return;
    const receive = (event: MessageEvent) => {
      const reply = event.data as { success?: boolean; code?: string; message?: string; activeDrawing?: string };
      if (!reply || typeof reply !== "object" || !reply.code) return;
      setMessage(`${reply.success ? "CAD" : reply.code}: ${reply.message || "Plugin response"}${reply.activeDrawing ? ` · ${reply.activeDrawing.split(/[\\/]/).pop()}` : ""}`);
    };
    webview.addEventListener("message", receive);
    return () => webview.removeEventListener?.("message", receive);
  }, []);

  const activeGraph = mode === "design_study" && draft ? draft.graph : base?.graph;
  const nodes = activeGraph?.["@graph"] || [];
  const spaces = nodes.filter((node) => SPACE_TYPES.has(node["@type"]));
  const walls = nodes.filter((node) => node["@type"] === "top:Wall");
  const portals = nodes.filter((node) => PORTAL_TYPES.has(node["@type"]));
  const selected = nodes.find((node) => node["@id"] === selectedId);
  const diagnostics = mode === "design_study" && draft ? draft.diagnostics : base?.diagnostics || [];
  const failedIds = useMemo(() => new Set(diagnostics.filter((item) => item.severity === "error").flatMap((item) => item.affected)), [diagnostics]);
  const activeCommands = useMemo(() => flattenCommands(actions, undoPosition), [actions, undoPosition]);
  const activeChanges = useMemo(() => flattenChanges(actions, undoPosition), [actions, undoPosition]);

  const confirmedDiscoveryIds = useMemo(() => {
    const set = new Set<string>();
    for (const node of nodes) {
      const discId = node["cad:properties"]?.source_discovery_id || node["cad:properties"]?.source_candidate_id;
      if (discId) set.add(String(discId));
    }
    return set;
  }, [nodes]);

  const activeDiscoveries = useMemo(() => {
    const raw = base?.discoveries || [];
    return raw.filter((d) => !confirmedDiscoveryIds.has(d.discovery_id) && !ignoredDiscoveryIds.has(d.discovery_id));
  }, [base?.discoveries, confirmedDiscoveryIds, ignoredDiscoveryIds]);

  const persist = useCallback(async (nextActions: EditorAction[], nextPosition: number) => {
    if (!base) return;
    setMessage("Evaluating Design Study…");
    try {
      const result = await storeDraft(base, nextActions, nextPosition, requiredWidth);
      setDraft(result);
      setPreview(null);
      setAcknowledged(false);
      setMessage(`Design Study saved · ${result.affected_ids.length} affected entities`);
    } catch (error) {
      setMessage(`Design Study error: ${error instanceof Error ? error.message : String(error)}`);
    }
  }, [base, requiredWidth]);

  const commitAction = useCallback(async (action: EditorAction) => {
    const next = [...actions.slice(0, undoPosition), action].slice(-100);
    setActions(next);
    setUndoPosition(next.length);
    await persist(next, next.length);
  }, [actions, undoPosition, persist]);

  const requestSelection = (id: string | null) => localFormDirty ? setPendingSelection(id) : setSelectedId(id);

  const enterDesignStudy = async () => {
    if (!base) return;
    setMode("design_study");
    setPreview(null);
    if (savedDraft && savedDraft.base_drawing_revision === base.drawing_revision && savedDraft.base_graph_revision === base.graph_revision) {
      setDraft(savedDraft);
      setActions(savedDraft.actions || []);
      setUndoPosition(savedDraft.undo_position);
      setMessage("Restored saved Design Study");
      return;
    }
    setActions([]);
    setUndoPosition(0);
    await persist([], 0);
  };

  const handleConfirmDiscovery = async (
    discovery: Discovery,
    customLabel?: string,
    customZone?: RoomZone,
    selectedEndpoints?: string[],
  ) => {
    if (mode !== "design_study") {
      await enterDesignStudy();
    }
    if (discovery.kind === "space") {
      const id = await digestId("urn:topospatial:space:", discovery.discovery_id);
      const label = customLabel || discovery.suggested_label || `Space ${spaces.length + 1}`;
      const zone = customZone || (discovery.suggested_zone as RoomZone) || "unassigned";
      const node: GraphNode = {
        "@id": id,
        "@type": "top:Room",
        "rdfs:label": label,
        "cad:managed": true,
        "cad:geometry": discovery.geometry || {},
        "cad:properties": {
          zone,
          source_discovery_id: discovery.discovery_id,
          source_handles: discovery.source_handles,
          source_layers: discovery.source_layers,
          confidence: discovery.confidence,
          spatial_program: {
            schema_version: 1,
            space_type: (discovery.suggested_type as SpaceType) || "other",
            custom_space_type: null,
            zone,
            intended_use: "",
            area_targets_m2: { minimum: null, target: discovery.area_m2 || null, maximum: null },
            occupancy: { design_count: null, accessible_required: true },
            privacy: "private",
            environment: { daylight: "preferred", ventilation: "natural_preferred", exterior_access: "none" },
            circulation: { is_entry: false, egress_role: "none", minimum_clear_width_mm: null },
            acoustic_separation: "none",
            architect_notes: "",
          },
        },
      };
      await commitAction({
        action_id: newActionId(),
        description: `Confirm room discovery: ${label}`,
        commands: [{ op: "upsert_node", semantic_id: id, node }],
        topology_changes: [
          {
            op: "annotate",
            "@id": id,
            "@type": "top:Room",
            label,
            targets: { candidate_id: discovery.discovery_id },
          },
        ],
      });
      setSelectedId(id);
    } else {
      const endpointIds = selectedEndpoints || discovery.endpoint_candidates || [];
      const id = await digestId("urn:topospatial:portal:", discovery.discovery_id);
      const label = customLabel || discovery.suggested_label || `Door ${portals.length + 1}`;
      const node: GraphNode = {
        "@id": id,
        "@type": discovery.kind === "opening" ? "top:Opening" : "top:Door",
        "rdfs:label": label,
        "cad:managed": true,
        "cad:geometry": {
          ...(discovery.geometry || {}),
          ...(discovery.host_wall_candidate ? { host_wall_id: discovery.host_wall_candidate } : {}),
          endpoint_space_ids: endpointIds,
        },
        "cad:boundingRooms": endpointIds,
        "cad:properties": {
          clear_width_mm: discovery.clear_width_mm || 900,
          source_discovery_id: discovery.discovery_id,
          source_handles: discovery.source_handles,
          source_layers: discovery.source_layers,
          confidence: discovery.confidence,
          endpoint_space_ids: endpointIds,
        },
      };
      await commitAction({
        action_id: newActionId(),
        description: `Confirm portal discovery: ${label}`,
        commands: [{ op: "upsert_node", semantic_id: id, node }],
        topology_changes: discovery.source_handles.length > 0 ? [
          {
            op: "annotate",
            "@id": id,
            "@type": discovery.kind === "opening" ? "top:Opening" : "top:Door",
            label,
            properties: {
              endpoint_space_ids: endpointIds,
              source_discovery_id: discovery.discovery_id,
            },
            targets: { handles: discovery.source_handles, grouping: "single_object" },
          },
        ] : [],
      });
      setSelectedId(id);
    }
  };

  const handleIgnoreDiscovery = (discoveryId: string) => {
    setIgnoredDiscoveryIds((prev) => new Set([...prev, discoveryId]));
  };

  const handleConfirmAllHighConfidence = async () => {
    if (mode !== "design_study") {
      await enterDesignStudy();
    }
    const highConfidence = activeDiscoveries.filter(
      (d) => d.confidence >= 0.75 && (d.kind === "space" || d.endpoint_candidates?.length === 2),
    );
    if (highConfidence.length === 0) return;

    const items = await Promise.all(
      highConfidence.map(async (discovery, index) => {
        if (discovery.kind === "space") {
          const id = await digestId("urn:topospatial:space:", discovery.discovery_id);
          const label = discovery.suggested_label || `Space ${spaces.length + index + 1}`;
          const zone = (discovery.suggested_zone as RoomZone) || "unassigned";
          const node: GraphNode = {
            "@id": id,
            "@type": "top:Room",
            "rdfs:label": label,
            "cad:managed": true,
            "cad:geometry": discovery.geometry || {},
            "cad:properties": {
              zone,
              source_discovery_id: discovery.discovery_id,
              source_handles: discovery.source_handles,
              source_layers: discovery.source_layers,
              confidence: discovery.confidence,
            },
          };
          return {
            command: { op: "upsert_node" as const, semantic_id: id, node },
            change: {
              op: "annotate" as const,
              "@id": id,
              "@type": "top:Room",
              label,
              targets: { candidate_id: discovery.discovery_id },
            },
          };
        } else {
          const id = await digestId("urn:topospatial:portal:", discovery.discovery_id);
          const label = discovery.suggested_label || `Portal ${portals.length + index + 1}`;
          const node: GraphNode = {
            "@id": id,
            "@type": "top:Door",
            "rdfs:label": label,
            "cad:managed": true,
            "cad:geometry": discovery.geometry || {},
            "cad:properties": {
              clear_width_mm: discovery.clear_width_mm || 900,
              source_discovery_id: discovery.discovery_id,
              source_handles: discovery.source_handles,
              endpoint_space_ids: discovery.endpoint_candidates || [],
            },
            "cad:boundingRooms": discovery.endpoint_candidates || [],
          };
          return {
            command: { op: "upsert_node" as const, semantic_id: id, node },
            change: {
              op: "annotate" as const,
              "@id": id,
              "@type": discovery.kind === "opening" ? "top:Opening" : "top:Door",
              label,
              properties: { endpoint_space_ids: discovery.endpoint_candidates || [] },
              targets: { handles: discovery.source_handles, grouping: "single_object" as const },
            },
          };
        }
      }),
    );

    await commitAction({
      action_id: newActionId(),
      description: `Confirm ${items.length} high-confidence discoveries`,
      commands: items.map((item) => item.command),
      topology_changes: items.map((item) => item.change),
    });
  };

  const addConnection = async () => {
    if (!connection.from || !connection.to || connection.from === connection.to) return;
    const topology_changes: TopologyChange[] = [];
    let via = connection.portal;
    if (connection.physical) {
      if (!connection.wall) return setMessage("Choose the host wall for the proposed door");
      via = `${await connectionId(connection.from, connection.to, `door:${connection.wall}`)}:door`;
      topology_changes.push({
        op: "create",
        "@id": via,
        "@type": "top:Door",
        representation: "auto",
        geometry: { host_wall_id: connection.wall, offset: 0, width: connection.width, height: 2100, hinge: "left", swing: "in" },
      });
    }
    const id = await connectionId(connection.from, connection.to, via);
    const fromName = displayName(nodes.find((n) => n["@id"] === connection.from));
    const toName = displayName(nodes.find((n) => n["@id"] === connection.to));
    const node: GraphNode = {
      "@id": id,
      "@type": "top:Connection",
      "rdfs:label": `${fromName} to ${toName}`,
      "cad:managed": true,
      "cad:geometry": {
        from_space_id: connection.from,
        to_space_id: connection.to,
        via_id: via,
        clear_width_mm: connection.width,
        direction: connection.direction,
        accessible: connection.accessible,
        status: "confirmed",
        source: "explicit",
      },
    };
    await commitAction({
      action_id: newActionId(),
      description: `Add circulation from ${fromName} to ${toName}`,
      commands: [{ op: "upsert_node", semantic_id: id, node }],
      topology_changes,
    });
    setSelectedId(id);
  };

  const undo = async () => { const next = Math.max(0, undoPosition - 1); setUndoPosition(next); await persist(actions, next); };
  const redo = async () => { const next = Math.min(actions.length, undoPosition + 1); setUndoPosition(next); await persist(actions, next); };
  const reset = async () => {
    if (!base) return;
    if (preview) await cancelPreview(preview, base.drawing);
    await resetDraft(base.drawing);
    setActions([]);
    setUndoPosition(0);
    setDraft(null);
    setPreview(null);
    setMessage("Design Study reset · DWG unchanged");
  };
  const doPreview = async () => {
    if (!base || !draft || draft.frozen || draft.conflicts.length) return;
    try {
      const result = await previewDraft(base, activeCommands, activeChanges);
      setPreview(result);
      setMessage("Preview ready · DWG still unchanged");
    } catch (error) {
      setMessage(`Preview failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  };
  const doApply = async () => {
    if (!base || !preview || (diagnostics.length && !acknowledged)) return;
    try {
      await applyPreview(preview, base.drawing);
      setPreview(null);
      setDraft(null);
      setActions([]);
      setUndoPosition(0);
      setMode("current_drawing");
      await refresh(true);
      setMessage("Applied in one AutoCAD transaction");
    } catch (error) {
      setMessage(`Apply failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  };
  const doRebase = async () => {
    if (!base) return;
    const result = await rebaseDraft(base.drawing);
    setDraft(result);
    setBase({ ...base, drawing_revision: result.base_drawing_revision, graph_revision: result.base_graph_revision });
    setMessage(result.conflicts.length ? "Rebase found conflicts" : "Design Study rebased");
  };
  const zoomInCad = (node: GraphNode) => {
    const bridge = (window as unknown as { chrome?: { webview?: { postMessage: (message: unknown) => void } } }).chrome?.webview;
    if (!bridge || !base) return setMessage("CAD focus is available inside the AutoCAD palette");
    bridge.postMessage({ version: 1, action: "focus_entity", expectedDrawing: base.drawing, semanticId: node["@id"], handle: node["cad:handles"]?.[0] || "" });
    setMessage(`Requested focus for ${displayName(node)} in the pinned drawing`);
  };
  const workflowState = draft?.frozen ? "frozen" : draft?.conflicts.length ? "conflicted" : preview ? "preview_ready" : localFormDirty ? "editing_local" : draft?.dirty ? "study_saved" : "clean";

  const isSelectedRelationship = selected && (selected["@type"] === "top:Connection" || selected["@type"] === "top:SpatialIntent");

  return (
    <main className="studio-root">
      <header className="studio-header">
        <div className="brand-group">
          <span className="brand-name">TopoSpatial Studio</span>
          <span className="version-tag">v{base?.backend?.studio_version || "0.3.0"}</span>
          <span className="drawing-pill">{base?.drawing?.split(/[\\/]/).pop() || "No drawing"}</span>
          <span className={`sync-pill ${syncStatus}`}><i className="sync-dot" />{syncStatus}</span>
        </div>
        <div className="status-message">{lastCadEvent ? `${lastCadEvent} · ` : ""}{message}</div>
        <nav className="mode-nav">
          <div className="mode-toggle-group" role="tablist">
            <button
              type="button"
              role="tab"
              className={`mode-btn ${mode === "current_drawing" ? "active" : ""}`}
              onClick={() => setMode("current_drawing")}
              title="Read-only visualization of verified CAD geometric topology"
            >
              Current Drawing
            </button>
            <button
              type="button"
              role="tab"
              className={`mode-btn ${mode === "design_study" ? "active" : ""}`}
              disabled={!base}
              onClick={() => void enterDesignStudy()}
              title="Safe in-memory exploration of space programs, doors, and spatial intents"
            >
              Design Study {draft?.dirty ? "●" : ""}
            </button>
          </div>
          <button type="button" className="refresh-btn" onClick={() => void refresh(false)}>Refresh</button>
        </nav>
      </header>

      {backendMismatch && (
        <div className="backend-mismatch-banner" role="alert">
          <strong>Backend mismatch:</strong> {backendMismatch}
        </div>
      )}

      {base?.unit_diagnostics?.length ? (
        <div className="unit-warning-banner" role="alert">
          <strong>Drawing units warning:</strong> {base.unit_diagnostics[0].message}
        </div>
      ) : null}

      {mode === "design_study" && (
        <div className={`sandbox-banner ${draft?.frozen ? "frozen" : ""}`}>
          <div className="sandbox-info">
            <strong>{draft?.frozen ? "Design Study frozen — CAD changed" : "Design Study — DWG unchanged"}</strong>
            <span>Pinned to {base?.drawing} · drawing {base?.drawing_revision.slice(0, 8)} · graph {base?.graph_revision.slice(0, 8)}</span>
          </div>
          {draft?.frozen && <button type="button" className="rebase-btn" onClick={() => void doRebase()}>Rebase</button>}
        </div>
      )}

      {mode === "current_drawing" && (
        <div className="mode-guide-banner">
          <span className="guide-icon">📐</span>
          <span>
            <strong>Current Drawing:</strong> Displaying verified CAD geometry and confirmed topology. Switch to <strong>Design Study</strong> to propose room programs, add doors, or define spatial intents safely in memory.
          </span>
        </div>
      )}

      <div className="compact-view-tabs">
        <button type="button" className={compactView === "plan" ? "active" : ""} onClick={() => setCompactView("plan")} title="View and select spaces on the CAD plan">Plan</button>
        <button type="button" className={compactView === "graph" ? "active" : ""} onClick={() => setCompactView("graph")} title="Inspect doors, connections, and spatial intent">Graph</button>
        <button type="button" className={compactView === "details" ? "active" : ""} onClick={() => setCompactView("details")} title="Edit the selected space and review staged changes">
          Details{selected ? " · 1" : ""}
        </button>
      </div>

      <section className={`studio-layout compact-workspace-${compactView}`}>
        <section className={`split-view compact-${compactView}`}>
          <div className="plan-pane">
            <div className="view-title">2D Plan</div>
            <PlanCanvas
              key={base?.drawing}
              nodes={nodes}
              planEntities={base?.cad_plan_entities || []}
              selectedId={selectedId}
              failedIds={failedIds}
              mode={mode}
              advancedGeometry={advancedGeometry}
              onSelect={requestSelection}
              onCommitAction={commitAction}
            />
          </div>
          <div className="graph-pane">
            <div className="view-title">Spatial Logic Graph</div>
            <GraphView
              nodes={nodes}
              discoveries={activeDiscoveries}
              selectedId={selectedId}
              failedIds={failedIds}
              mode={mode}
              onSelect={requestSelection}
              onCommitAction={commitAction}
            />
          </div>
        </section>

        <aside className="studio-sidebar">
          {isSelectedRelationship ? (
            <section className="panel inspector-panel">
              <RelationshipInspector
                node={selected!}
                allNodes={nodes}
                mode={mode}
                onCommitAction={commitAction}
                onClose={() => setSelectedId(null)}
              />
            </section>
          ) : (
            <SpaceInspector
              selected={selected}
              nodes={nodes}
              mode={mode}
              stagedCount={undoPosition}
              disabled={Boolean(draft?.frozen || draft?.conflicts.length)}
              onCommitAction={commitAction}
              onDirtyChange={setLocalFormDirty}
              onSaveHandler={(handler) => { inspectorSaveRef.current = handler; }}
              onFocusCad={() => selected && zoomInCad(selected)}
            />
          )}

          <SpaceProgramPanel nodes={nodes} selectedId={selectedId} mode={mode} onSelect={requestSelection} />

          {activeDiscoveries.length > 0 && (
            <section className="panel discoveries-container-panel">
              <DrawingDiscoveriesPanel
                discoveries={activeDiscoveries}
                nodes={nodes}
                onConfirmDiscovery={handleConfirmDiscovery}
                onIgnoreDiscovery={handleIgnoreDiscovery}
                onConfirmAllHighConfidence={handleConfirmAllHighConfidence}
              />
            </section>
          )}

          {mode === "design_study" && (
            <section className="panel staged-panel">
              <h2>Design Study Changes</h2>
              <div className="workflow-line">
                <strong>{workflowState.replace("_", " ")}</strong>
                <span>{undoPosition} of {actions.length} architect actions active</span>
              </div>
              <div className="button-row">
                <button type="button" disabled={undoPosition === 0} onClick={() => void undo()}>Undo action</button>
                <button type="button" disabled={undoPosition >= actions.length} onClick={() => void redo()}>Redo action</button>
                <button type="button" className="quiet" onClick={() => void reset()}>Reset draft</button>
              </div>
              {actions.slice(0, undoPosition).map((action) => (
                <div className="staged-action" key={action.action_id}>{action.description}</div>
              ))}
            </section>
          )}

          {mode === "design_study" && base && (
            <McpHandoffPanel
              drawing={base.drawing}
              draft={draft}
              nodes={nodes}
              disabled={Boolean(localFormDirty || draft?.frozen || draft?.conflicts.length)}
              onStatus={setMessage}
            />
          )}

          {mode === "design_study" && (
            <CirculationPanel
              connection={connection}
              setConnection={setConnection}
              spaces={spaces}
              walls={walls}
              portals={portals}
              frozen={Boolean(draft?.frozen)}
              onAdd={addConnection}
            />
          )}

          {mode === "design_study" && (
            <section className="panel advanced-panel">
              <label className="check">
                <input
                  type="checkbox"
                  checked={advancedGeometry}
                  onChange={(event) => setAdvancedGeometry(event.target.checked)}
                />
                Advanced Geometry
              </label>
              {advancedGeometry && (
                <p className="warning-text">Wall-junction grips change physical geometry in the Design Study. Preview is mandatory.</p>
              )}
            </section>
          )}

          <section className="panel diagnostics-panel">
            <h2>Diagnostics ({diagnostics.length})</h2>
            {diagnostics.length ? (
              diagnostics.map((issue, index) => (
                <button
                  type="button"
                  className={`issue ${issue.severity}`}
                  key={`${issue.code}:${index}`}
                  onClick={() => issue.affected[0] && requestSelection(issue.affected[0])}
                >
                  <strong>{issue.code}</strong>
                  <span>{issue.message}</span>
                </button>
              ))
            ) : (
              <p className="panel-desc clean">No current spatial-logic or clearance issues.</p>
            )}
          </section>

          <section className="panel route-panel">
            <h2>Route & Clearance</h2>
            <div className="form-row">
              <select value={routeEnds[0]} onChange={(e) => setRouteEnds([e.target.value, routeEnds[1]])}>
                <option value="">Origin…</option>
                {spaces.map(spaceOption)}
              </select>
              <select value={routeEnds[1]} onChange={(e) => setRouteEnds([routeEnds[0], e.target.value])}>
                <option value="">Destination…</option>
                {spaces.map(spaceOption)}
              </select>
            </div>
            <label>
              Required width (mm)
              <input type="number" min="0" value={requiredWidth} onChange={(e) => setRequiredWidth(+e.target.value)} />
            </label>
            <button
              type="button"
              disabled={!routeEnds[0] || !routeEnds[1]}
              onClick={async () => setRouteResult(await route(routeEnds[0], routeEnds[1], requiredWidth, base?.drawing, draft?.draft_revision))}
            >
              Evaluate route
            </button>
            {routeResult && <pre>{JSON.stringify(routeResult, null, 2)}</pre>}
          </section>

          {mode === "design_study" && (
            <section className="panel apply-panel">
              <h2>Preview / Apply</h2>
              {preview ? (
                <>
                  <p>Preview is revision-bound. DWG is still unchanged.</p>
                  {diagnostics.length > 0 && (
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={acknowledged}
                        onChange={(event) => setAcknowledged(event.target.checked)}
                      />
                      Acknowledge {diagnostics.length} advisory diagnostics
                    </label>
                  )}
                  <div className="button-row">
                    <button
                      type="button"
                      className="quiet"
                      onClick={async () => {
                        if (base) await cancelPreview(preview, base.drawing);
                        setPreview(null);
                      }}
                    >
                      Cancel preview
                    </button>
                    <button
                      type="button"
                      className="apply-btn"
                      disabled={diagnostics.length > 0 && !acknowledged}
                      onClick={() => void doApply()}
                    >
                      Apply to pinned DWG
                    </button>
                  </div>
                </>
              ) : (
                <button
                  type="button"
                  className="preview-btn"
                  disabled={!draft?.dirty || localFormDirty || draft?.frozen || Boolean(draft?.conflicts.length)}
                  onClick={() => void doPreview()}
                >
                  Preview Design Study
                </button>
              )}
            </section>
          )}
        </aside>
      </section>

      {pendingSelection !== undefined && (
        <div className="modal-backdrop">
          <div className="selection-modal">
            <h2>Unsaved space edits</h2>
            <p>Save this space program before changing selection?</p>
            <div className="button-row">
              <button
                type="button"
                className="primary-action-btn"
                onClick={async () => {
                  await inspectorSaveRef.current?.();
                  setSelectedId(pendingSelection);
                  setPendingSelection(undefined);
                }}
              >
                Save and continue
              </button>
              <button
                type="button"
                onClick={() => {
                  setLocalFormDirty(false);
                  setSelectedId(pendingSelection);
                  setPendingSelection(undefined);
                }}
              >
                Discard
              </button>
              <button
                type="button"
                className="quiet"
                onClick={() => setPendingSelection(undefined)}
              >
                Stay
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

type ConnectionState = { from: string; to: string; direction: Direction; width: number; accessible: boolean; portal: string; wall: string; physical: boolean };
function CirculationPanel({
  connection,
  setConnection,
  spaces,
  walls,
  portals,
  frozen,
  onAdd,
}: {
  connection: ConnectionState;
  setConnection: (next: ConnectionState) => void;
  spaces: GraphNode[];
  walls: GraphNode[];
  portals: GraphNode[];
  frozen: boolean;
  onAdd: () => Promise<void>;
}) {
  return (
    <section className="panel">
      <h2>Actual Circulation</h2>
      <p className="panel-desc">Create a real logical path or portal-backed connection. Use Relationships above for design intent.</p>
      <label>
        From
        <select value={connection.from} onChange={(e) => setConnection({ ...connection, from: e.target.value })}>
          <option value="">Choose…</option>
          {spaces.map(spaceOption)}
        </select>
      </label>
      <label>
        To
        <select value={connection.to} onChange={(e) => setConnection({ ...connection, to: e.target.value })}>
          <option value="">Choose…</option>
          {spaces.map(spaceOption)}
        </select>
      </label>
      <div className="form-row">
        <label>
          Direction
          <select value={connection.direction} onChange={(e) => setConnection({ ...connection, direction: e.target.value as Direction })}>
            <option value="bidirectional">bidirectional</option>
            <option value="forward">forward</option>
            <option value="reverse">reverse</option>
          </select>
        </label>
        <label>
          Clear width
          <input type="number" min="0" value={connection.width} onChange={(e) => setConnection({ ...connection, width: +e.target.value })} />
        </label>
      </div>
      <label className="check">
        <input type="checkbox" checked={connection.accessible} onChange={(e) => setConnection({ ...connection, accessible: e.target.checked })} />
        Accessible
      </label>
      <label className="check">
        <input type="checkbox" checked={connection.physical} onChange={(e) => setConnection({ ...connection, physical: e.target.checked })} />
        Propose one new hosted door
      </label>
      {connection.physical ? (
        <label>
          Host wall
          <select value={connection.wall} onChange={(e) => setConnection({ ...connection, wall: e.target.value })}>
            <option value="">Choose wall…</option>
            {walls.map(spaceOption)}
          </select>
        </label>
      ) : (
        <label>
          Existing portal
          <select value={connection.portal} onChange={(e) => setConnection({ ...connection, portal: e.target.value })}>
            <option value="">Logical only</option>
            {portals.map(spaceOption)}
          </select>
        </label>
      )}
      <button
        type="button"
        className="primary-action-btn"
        disabled={!connection.from || !connection.to || connection.from === connection.to || frozen}
        onClick={() => void onAdd()}
      >
        Add actual connection
      </button>
    </section>
  );
}

function spaceOption(node: GraphNode) {
  return (
    <option key={node["@id"]} value={node["@id"]}>
      {displayName(node)}
    </option>
  );
}
