import { useMemo, useState } from "react";
import { displayName, spatialIntentId } from "./spatial";
import type { Direction, EditorAction, GraphNode, IntentKind, IntentPriority, PortalRequirement } from "./types";

interface Props {
  selected: GraphNode;
  nodes: GraphNode[];
  disabled: boolean;
  onCommitAction: (action: EditorAction) => Promise<void> | void;
}

export function SpatialIntentEditor({ selected, nodes, disabled, onCommitAction }: Props) {
  const spaces = useMemo(
    () => nodes.filter((node) => ["top:Room", "top:Space"].includes(node["@type"]) && node["@id"] !== selected["@id"]),
    [nodes, selected],
  );
  const [target, setTarget] = useState("");
  const [kind, setKind] = useState<IntentKind>("adjacent");
  const [priority, setPriority] = useState<IntentPriority>("should");
  const [direction, setDirection] = useState<Direction>("bidirectional");
  const [portalRequirement, setPortalRequirement] = useState<PortalRequirement>("none");
  const [minimumWidth, setMinimumWidth] = useState("");
  const [rationale, setRationale] = useState("");

  const save = async () => {
    if (!target || target === selected["@id"]) return;
    const id = await spatialIntentId(selected["@id"], target, kind, direction);
    const targetNode = nodes.find((node) => node["@id"] === target);
    const node: GraphNode = {
      "@id": id,
      "@type": "top:SpatialIntent",
      "rdfs:label": `${displayName(selected)} ${priority.replace("_", " ")} ${kind.replace("_", " ")} ${displayName(targetNode)}`,
      "cad:managed": true,
      "cad:properties": {
        intent_schema_version: 1,
        source_space_id: selected["@id"],
        target_space_id: target,
        kind,
        priority,
        direction,
        minimum_clear_width_mm: minimumWidth === "" ? null : Number(minimumWidth),
        portal_requirement: portalRequirement,
        rationale: rationale.trim(),
        evaluation_status: "unverified",
      },
    };
    await onCommitAction({
      action_id: crypto.randomUUID(),
      description: `Add ${kind.replace("_", " ")} intent from ${displayName(selected)} to ${displayName(targetNode)}`,
      commands: [{ op: "upsert_node", semantic_id: id, node }],
      topology_changes: [],
    });
    setRationale("");
  };

  return (
    <div className="intent-editor">
      <p className="panel-desc">Describe what should be true. This does not create a door or circulation path.</p>
      <label>Related space<select value={target} onChange={(event) => setTarget(event.target.value)} disabled={disabled}>
        <option value="">Choose a space…</option>
        {spaces.map((space) => <option key={space["@id"]} value={space["@id"]}>{displayName(space)}</option>)}
      </select></label>
      <div className="form-row">
        <label className="flex-1">Relationship<select value={kind} onChange={(event) => setKind(event.target.value as IntentKind)} disabled={disabled}>
          <option value="adjacent">Adjacent</option><option value="direct_access">Direct access</option>
          <option value="near">Near</option><option value="separated">Separated</option>
          <option value="visual_connection">Visual connection</option><option value="acoustic_separation">Acoustic separation</option>
          <option value="service_dependency">Service dependency</option><option value="sequence">Sequence</option>
        </select></label>
        <label className="flex-1">Priority<select value={priority} onChange={(event) => setPriority(event.target.value as IntentPriority)} disabled={disabled}>
          <option value="must">Must</option><option value="should">Should</option><option value="prefer">Prefer</option>
          <option value="avoid">Avoid</option><option value="must_not">Must not</option>
        </select></label>
      </div>
      <div className="form-row">
        <label className="flex-1">Direction<select value={direction} onChange={(event) => setDirection(event.target.value as Direction)} disabled={disabled}>
          <option value="bidirectional">Bidirectional</option><option value="forward">Forward</option><option value="reverse">Reverse</option>
        </select></label>
        <label className="flex-1">Portal requirement<select value={portalRequirement} onChange={(event) => setPortalRequirement(event.target.value as PortalRequirement)} disabled={disabled}>
          <option value="none">None</option><option value="existing_portal">Existing portal</option><option value="door">Door</option>
          <option value="opening">Opening</option><option value="any_portal">Any portal</option>
        </select></label>
      </div>
      <label>Minimum clear width (mm)<input type="number" min="0" step="50" value={minimumWidth} onChange={(event) => setMinimumWidth(event.target.value)} disabled={disabled} /></label>
      <label>Architect rationale<textarea maxLength={1000} value={rationale} onChange={(event) => setRationale(event.target.value)} disabled={disabled} /></label>
      <button className="primary-action-btn" type="button" disabled={disabled || !target} onClick={() => void save()}>Save relationship to Design Study</button>
    </div>
  );
}
