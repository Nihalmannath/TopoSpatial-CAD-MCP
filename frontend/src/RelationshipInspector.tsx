import type { Direction, EditorAction, GraphNode, StudioMode } from "./types";
import { displayName } from "./spatial";
import { DOOR_PRESETS } from "./archUtils";

interface Props {
  node: GraphNode;
  allNodes: GraphNode[];
  mode: StudioMode;
  onCommitAction?: (action: EditorAction) => Promise<void> | void;
  onClose: () => void;
}

const newActionId = () => crypto.randomUUID();

export function RelationshipInspector({
  node,
  allNodes,
  mode,
  onCommitAction,
  onClose,
}: Props) {
  const isConnection = node["@type"] === "top:Connection";
  const isIntent = node["@type"] === "top:SpatialIntent";
  const isDesignStudy = mode === "design_study";

  const geom = node["cad:geometry"] || {};
  const props = node["cad:properties"] || {};

  const fromId = isConnection ? geom.from_space_id : props.source_space_id || geom.from_space_id;
  const toId = isConnection ? geom.to_space_id : props.target_space_id || geom.to_space_id;

  const fromNode = allNodes.find((n) => n["@id"] === fromId);
  const toNode = allNodes.find((n) => n["@id"] === toId);

  const fromLabel = fromId === "urn:topospatial:space:outside" ? "Exterior Façade" : displayName(fromNode);
  const toLabel = toId === "urn:topospatial:space:outside" ? "Exterior Façade" : displayName(toNode);

  const widthMm = Number(geom.clear_width_mm || 900);
  const isFaçade = fromId === "urn:topospatial:space:outside" || toId === "urn:topospatial:space:outside";
  const direction = (geom.direction as Direction) || "bidirectional";

  const handleUpdateWidth = async (newWidth: number) => {
    if (!onCommitAction) return;
    await onCommitAction({
      action_id: newActionId(),
      description: `Update clear width to ${newWidth}mm`,
      commands: [
        {
          op: "patch_node",
          semantic_id: node["@id"],
          patch: {
            "cad:geometry": {
              ...geom,
              clear_width_mm: newWidth,
              accessible: newWidth >= 900,
            },
          },
        },
      ],
      topology_changes: [],
    });
  };

  const handleToggleDirection = async () => {
    if (!onCommitAction) return;
    const nextDir: Direction = direction === "bidirectional" ? "forward" : direction === "forward" ? "reverse" : "bidirectional";
    await onCommitAction({
      action_id: newActionId(),
      description: `Change circulation direction to ${nextDir}`,
      commands: [
        {
          op: "patch_node",
          semantic_id: node["@id"],
          patch: {
            "cad:geometry": {
              ...geom,
              direction: nextDir,
            },
          },
        },
      ],
      topology_changes: [],
    });
  };

  const handleDelete = async () => {
    if (!onCommitAction) return;
    await onCommitAction({
      action_id: newActionId(),
      description: `Remove ${isConnection ? "door connection" : "spatial intent"}`,
      commands: [{ op: "delete_node", semantic_id: node["@id"] }],
      topology_changes: [{ op: "delete", "@id": node["@id"] }],
    });
    onClose();
  };

  return (
    <div className="relationship-inspector">
      <div className="inspector-header">
        <div className="title-area">
          <span className={`type-tag ${isConnection ? "connection" : "intent"}`}>
            {isConnection ? (isFaçade ? "🚪 Façade Exit Portal" : "🚪 Door / Opening") : "⚯ Spatial Intent"}
          </span>
          <h4>{node["rdfs:label"] || "Relationship"}</h4>
        </div>
        <button type="button" className="close-btn" onClick={onClose}>
          ✕
        </button>
      </div>

      <div className="relationship-endpoints">
        <div className="endpoint-box">
          <span className="ep-label">From:</span>
          <strong>{fromLabel}</strong>
        </div>
        <div className="direction-indicator">
          {direction === "bidirectional" ? "⟷" : direction === "forward" ? "⟶" : "⟵"}
        </div>
        <div className="endpoint-box">
          <span className="ep-label">To:</span>
          <strong>{toLabel}</strong>
        </div>
      </div>

      {isConnection && (
        <div className="inspector-section">
          <h5>Circulation & Clearance</h5>
          <div className="metric-grid">
            <div className="metric-tile">
              <span className="tile-label">Clear Width</span>
              <span className="tile-value">{Math.round(widthMm)} mm</span>
            </div>
            <div className="metric-tile">
              <span className="tile-label">Accessibility</span>
              <span className={`tile-value ${widthMm >= 900 ? "success" : "warning"}`}>
                {widthMm >= 900 ? "✓ Universal (≥900mm)" : "⚠ Narrow (<900mm)"}
              </span>
            </div>
            <div className="metric-tile">
              <span className="tile-label">Directionality</span>
              <span className="tile-value">{direction}</span>
            </div>
            <div className="metric-tile">
              <span className="tile-label">Façade Exit</span>
              <span className={`tile-value ${isFaçade ? "success" : ""}`}>
                {isFaçade ? "Yes (Building Façade)" : "Interior Partition"}
              </span>
            </div>
          </div>

          {isDesignStudy && (
            <div className="inspector-actions-group">
              <label>Preset Width:</label>
              <div className="preset-buttons">
                {DOOR_PRESETS.map((preset) => (
                  <button
                    key={preset.width}
                    type="button"
                    className={`preset-btn ${widthMm === preset.width ? "active" : ""}`}
                    onClick={() => handleUpdateWidth(preset.width)}
                  >
                    {preset.width}mm
                  </button>
                ))}
              </div>

              <div className="action-row-buttons">
                <button type="button" className="secondary-btn" onClick={handleToggleDirection}>
                  Toggle Flow Direction
                </button>
                <button type="button" className="danger-btn" onClick={handleDelete}>
                  Delete Connection
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {isIntent && (
        <div className="inspector-section">
          <h5>Programmatic Intent</h5>
          <div className="metric-grid">
            <div className="metric-tile">
              <span className="tile-label">Kind</span>
              <span className="tile-value">{String(props.kind || "adjacent").replace("_", " ")}</span>
            </div>
            <div className="metric-tile">
              <span className="tile-label">Priority</span>
              <span className="tile-value">{props.priority || "should"}</span>
            </div>
          </div>

          {isDesignStudy && (
            <div className="action-row-buttons">
              <button type="button" className="danger-btn" onClick={handleDelete}>
                Delete Intent
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
