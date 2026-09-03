import { useEffect, useMemo, useState } from "react";
import { extractRoomMetrics } from "./archUtils";
import { displayName, spatialProgramFromNode } from "./spatial";
import { SpatialIntentEditor } from "./SpatialIntentEditor";
import type { EditorAction, GraphNode, RoomZone, SpaceType, SpatialProgram, StudioMode } from "./types";

type Tab = "program" | "relationships" | "access" | "environment" | "notes";

interface Props {
  selected: GraphNode | undefined;
  nodes: GraphNode[];
  mode: StudioMode;
  stagedCount: number;
  disabled: boolean;
  onCommitAction: (action: EditorAction) => Promise<void> | void;
  onDirtyChange: (dirty: boolean) => void;
  onSaveHandler: (handler: (() => Promise<void>) | null) => void;
  onFocusCad: (handle: string) => void;
}

const SPACE_TYPES = new Set(["top:Room", "top:Space"]);
const SPACE_OPTIONS: SpaceType[] = ["living_room", "dining_room", "kitchen", "bedroom", "bathroom", "toilet", "corridor", "lobby", "staircase", "office", "storage", "utility", "balcony", "terrace", "parking", "outdoor", "other"];
const ZONES: RoomZone[] = ["living", "sleeping", "service", "circulation", "office", "outdoor", "unassigned"];

function optionalNumber(value: string): number | null {
  return value.trim() === "" ? null : Number(value);
}

function profileError(profile: SpatialProgram): string | null {
  if (profile.space_type === "other" && !profile.custom_space_type?.trim()) return "Describe the custom space type.";
  const { minimum, target, maximum } = profile.area_targets_m2;
  const values = [minimum, target, maximum, profile.circulation.minimum_clear_width_mm];
  if (values.some((value) => value != null && (!Number.isFinite(value) || value < 0))) return "Areas and widths must be finite, non-negative numbers.";
  if (profile.occupancy.design_count != null && (!Number.isInteger(profile.occupancy.design_count) || profile.occupancy.design_count < 0 || profile.occupancy.design_count > 10000)) return "Occupancy must be a whole number from 0 to 10,000.";
  if (minimum != null && target != null && minimum > target) return "Minimum area cannot exceed target area.";
  if (target != null && maximum != null && target > maximum) return "Target area cannot exceed maximum area.";
  if (minimum != null && maximum != null && minimum > maximum) return "Minimum area cannot exceed maximum area.";
  return null;
}

export function SpaceInspector({ selected, nodes, mode, stagedCount, disabled, onCommitAction, onDirtyChange, onSaveHandler, onFocusCad }: Props) {
  const [tab, setTab] = useState<Tab>("program");
  const [name, setName] = useState("");
  const [profile, setProfile] = useState<SpatialProgram | null>(null);
  const [savedSignature, setSavedSignature] = useState("");
  const isSpace = Boolean(selected && SPACE_TYPES.has(selected["@type"]));

  useEffect(() => {
    if (!selected || !SPACE_TYPES.has(selected["@type"])) {
      setProfile(null);
      setName("");
      setSavedSignature("");
      onDirtyChange(false);
      return;
    }
    const nextName = displayName(selected);
    const nextProfile = spatialProgramFromNode(selected);
    setName(nextName);
    setProfile(nextProfile);
    setSavedSignature(JSON.stringify({ name: nextName, profile: nextProfile }));
    setTab("program");
    onDirtyChange(false);
  }, [selected?.["@id"]]);

  const currentSignature = useMemo(() => JSON.stringify({ name: name.trim(), profile }), [name, profile]);
  const dirty = Boolean(profile && currentSignature !== savedSignature);
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const metrics = selected && isSpace ? extractRoomMetrics(selected, nodes) : null;
  const error = profile && (name.trim().length < 1 || name.trim().length > 128) ? "Display name must contain 1–128 characters." : profile ? profileError(profile) : null;
  const update = (patch: Partial<SpatialProgram>) => setProfile((current) => current ? { ...current, ...patch } : current);
  const save = async () => {
    if (!selected || !profile || error || disabled) return;
    const updated: GraphNode = {
      ...selected,
      "rdfs:label": name.trim(),
      "cad:properties": { ...(selected["cad:properties"] || {}), zone: profile.zone, spatial_program: profile },
    };
    await onCommitAction({
      action_id: crypto.randomUUID(),
      description: `Update program for ${name.trim()}`,
      commands: [{ op: "upsert_node", semantic_id: selected["@id"], node: updated }],
      topology_changes: [],
    });
    setSavedSignature(JSON.stringify({ name: name.trim(), profile }));
  };

  useEffect(() => {
    onSaveHandler(dirty && !error && !disabled ? save : null);
    return () => onSaveHandler(null);
  }, [dirty, error, disabled, currentSignature, selected?.["@id"]]);

  if (!selected) return <section className="panel inspector-panel"><h2>Selected Space</h2><p className="panel-desc empty">Select a named space in the plan, graph, or schedule.</p></section>;
  if (!isSpace || !profile || !metrics) return (
    <section className="panel inspector-panel"><h2>{displayName(selected)}</h2><p className="panel-desc">{selected["@type"].replace("top:", "")} selected.</p>
      {selected["cad:handles"]?.[0] && <button type="button" onClick={() => onFocusCad(selected["cad:handles"]![0])}>Focus in CAD</button>}
      <details><summary>Advanced identity and JSON</summary><code>{selected["@id"]}</code><pre>{JSON.stringify(selected, null, 2)}</pre></details>
    </section>
  );

  return <section className="panel inspector-panel space-inspector">
    <header className="space-header">
      <div><h2>{displayName(selected)}</h2><p>{profile.space_type.replaceAll("_", " ")} · {profile.zone} · {profile.privacy.replace("_", " ")}</p></div>
      <strong>{metrics.areaM2.toFixed(1)} m²</strong>
    </header>
    <div className="sandbox-count">Design Study: {stagedCount} staged {stagedCount === 1 ? "action" : "actions"}</div>
    <div className="inspector-tabs" role="tablist">
      {(["program", "relationships", "access", "environment", "notes"] as Tab[]).map((item) => <button key={item} type="button" className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{item[0].toUpperCase() + item.slice(1)}</button>)}
    </div>
    <fieldset disabled={mode !== "design_study" || disabled}>
      {tab === "program" && <div className="inspector-form">
        <label>Display name<input value={name} maxLength={128} onChange={(event) => setName(event.target.value)} /></label>
        <div className="form-row"><label className="flex-1">Space type<select value={profile.space_type} onChange={(event) => update({ space_type: event.target.value as SpaceType })}>{SPACE_OPTIONS.map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
        <label className="flex-1">Zone<select value={profile.zone} onChange={(event) => update({ zone: event.target.value as RoomZone })}>{ZONES.map((value) => <option key={value}>{value}</option>)}</select></label></div>
        {profile.space_type === "other" && <label>Custom space type<input value={profile.custom_space_type || ""} onChange={(event) => update({ custom_space_type: event.target.value })} /></label>}
        <label>Intended use<textarea maxLength={500} value={profile.intended_use} onChange={(event) => update({ intended_use: event.target.value })} /></label>
        <div className="form-row"><label>Min area m²<input type="number" min="0" value={profile.area_targets_m2.minimum ?? ""} onChange={(event) => update({ area_targets_m2: { ...profile.area_targets_m2, minimum: optionalNumber(event.target.value) } })} /></label><label>Target m²<input type="number" min="0" value={profile.area_targets_m2.target ?? ""} onChange={(event) => update({ area_targets_m2: { ...profile.area_targets_m2, target: optionalNumber(event.target.value) } })} /></label><label>Max m²<input type="number" min="0" value={profile.area_targets_m2.maximum ?? ""} onChange={(event) => update({ area_targets_m2: { ...profile.area_targets_m2, maximum: optionalNumber(event.target.value) } })} /></label></div>
        <div className="form-row"><label className="flex-1">Occupancy<input type="number" min="0" max="10000" step="1" value={profile.occupancy.design_count ?? ""} onChange={(event) => update({ occupancy: { ...profile.occupancy, design_count: optionalNumber(event.target.value) } })} /></label><label className="flex-1">Privacy<select value={profile.privacy} onChange={(event) => update({ privacy: event.target.value as SpatialProgram["privacy"] })}><option value="public">public</option><option value="semi_public">semi public</option><option value="private">private</option><option value="service">service</option></select></label></div>
      </div>}
      {tab === "relationships" && <SpatialIntentEditor selected={selected} nodes={nodes} disabled={disabled || mode !== "design_study"} onCommitAction={onCommitAction} />}
      {tab === "access" && <div className="inspector-form">
        <label className="check"><input type="checkbox" checked={profile.occupancy.accessible_required} onChange={(event) => update({ occupancy: { ...profile.occupancy, accessible_required: event.target.checked } })} />Accessible space required</label>
        <label className="check"><input type="checkbox" checked={profile.circulation.is_entry} onChange={(event) => update({ circulation: { ...profile.circulation, is_entry: event.target.checked } })} />Building/zone entrance</label>
        <label>Egress role<select value={profile.circulation.egress_role} onChange={(event) => update({ circulation: { ...profile.circulation, egress_role: event.target.value as SpatialProgram["circulation"]["egress_role"] } })}><option value="none">none</option><option value="primary">primary</option><option value="secondary">secondary</option></select></label>
        <label>Minimum clear width (mm)<input type="number" min="0" step="50" value={profile.circulation.minimum_clear_width_mm ?? ""} onChange={(event) => update({ circulation: { ...profile.circulation, minimum_clear_width_mm: optionalNumber(event.target.value) } })} /></label>
      </div>}
      {tab === "environment" && <div className="inspector-form">
        <label>Daylight<select value={profile.environment.daylight} onChange={(event) => update({ environment: { ...profile.environment, daylight: event.target.value as SpatialProgram["environment"]["daylight"] } })}><option value="none">none</option><option value="preferred">preferred</option><option value="required">required</option></select></label>
        <label>Ventilation<select value={profile.environment.ventilation} onChange={(event) => update({ environment: { ...profile.environment, ventilation: event.target.value as SpatialProgram["environment"]["ventilation"] } })}><option value="none">none</option><option value="mechanical">mechanical</option><option value="natural_preferred">natural preferred</option><option value="natural_required">natural required</option></select></label>
        <label>Exterior access<select value={profile.environment.exterior_access} onChange={(event) => update({ environment: { ...profile.environment, exterior_access: event.target.value as SpatialProgram["environment"]["exterior_access"] } })}><option value="none">none</option><option value="preferred">preferred</option><option value="required">required</option></select></label>
        <label>Acoustic separation<select value={profile.acoustic_separation} onChange={(event) => update({ acoustic_separation: event.target.value as SpatialProgram["acoustic_separation"] })}><option value="none">none</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></label>
      </div>}
      {tab === "notes" && <div className="inspector-form"><label>Architect notes<textarea rows={7} maxLength={4000} value={profile.architect_notes} onChange={(event) => update({ architect_notes: event.target.value })} /></label></div>}
    </fieldset>
    {error && <p className="form-error">{error}</p>}
    {mode === "design_study" && tab !== "relationships" && <button type="button" className="primary-action-btn sticky-save" disabled={!dirty || Boolean(error) || disabled} onClick={() => void save()}>{dirty ? "Save to Design Study" : "Saved in Design Study"}</button>}
    <details className="raw-data-details"><summary>Advanced identity and raw JSON</summary><code>{selected["@id"]}</code>{selected["cad:handles"]?.[0] && <button type="button" onClick={() => onFocusCad(selected["cad:handles"]![0])}>Focus in CAD</button>}<pre>{JSON.stringify(selected, null, 2)}</pre></details>
  </section>;
}
