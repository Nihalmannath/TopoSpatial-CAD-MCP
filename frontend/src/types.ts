export type Point = [number, number];
export type Direction = "bidirectional" | "forward" | "reverse";

export type StudioMode = "current_drawing" | "design_study";
export type LayoutMode = "floor_plan" | "connectivity_map";
export type GraphTool = "select" | "intent" | "door" | "inspect";
export type RelationshipCategory =
  | "spatial_intent"
  | "portal_connectivity"
  | "geometric_adjacency";

export type RoomZone =
  | "living"
  | "sleeping"
  | "service"
  | "circulation"
  | "outdoor"
  | "office"
  | "unassigned";

export type DoorType =
  | "single_swing"
  | "double_swing"
  | "sliding"
  | "cased_opening";

export type ActiveTool = "select" | "wall_edit" | "measure" | "add_door";

export type SpaceType =
  | "living_room" | "dining_room" | "kitchen" | "bedroom" | "bathroom"
  | "toilet" | "corridor" | "lobby" | "staircase" | "office" | "storage"
  | "utility" | "balcony" | "terrace" | "parking" | "outdoor" | "other";
export type PrivacyLevel = "public" | "semi_public" | "private" | "service";
export type IntentKind = "adjacent" | "direct_access" | "near" | "separated" | "visual_connection" | "acoustic_separation" | "service_dependency" | "sequence";
export type IntentPriority = "must" | "should" | "prefer" | "avoid" | "must_not";
export type PortalRequirement = "none" | "existing_portal" | "door" | "opening" | "any_portal";

export interface SpatialProgram {
  schema_version: 1;
  space_type: SpaceType;
  custom_space_type: string | null;
  zone: RoomZone;
  intended_use: string;
  area_targets_m2: { minimum: number | null; target: number | null; maximum: number | null };
  occupancy: { design_count: number | null; accessible_required: boolean };
  privacy: PrivacyLevel;
  environment: {
    daylight: "none" | "preferred" | "required";
    ventilation: "none" | "mechanical" | "natural_preferred" | "natural_required";
    exterior_access: "none" | "preferred" | "required";
  };
  circulation: {
    is_entry: boolean;
    egress_role: "none" | "primary" | "secondary";
    minimum_clear_width_mm: number | null;
  };
  acoustic_separation: "none" | "low" | "medium" | "high";
  architect_notes: string;
}

export interface Discovery {
  discovery_id: string;
  kind: "space" | "door" | "opening";
  source_handles: string[];
  source_layers: string[];
  geometry: Record<string, any>;
  confidence: number;
  evidence: string[];
  suggested_label?: string | null;
  suggested_type?: string | null;
  suggested_zone?: string | null;
  area_m2?: number | null;
  clear_width_mm?: number | null;
  host_wall_candidate?: string | null;
  endpoint_candidates?: string[];
  status: "pending" | "confirmed" | "ignored" | "ambiguous";
  reason?: string | null;
  storey_id?: string | null;
  storey_name?: string | null;
  classification?:
    | "safe_to_confirm"
    | "needs_review"
    | "duplicate"
    | "nested"
    | "merged_or_oversized"
    | "storey_ambiguous"
    | "invalid"
    | null;
  classification_reason?: string | null;
  aspect_ratio?: number | null;
}

export interface GraphNode {
  "@id": string;
  "@type": string;
  "rdfs:label"?: string;
  "cad:geometry"?: Record<string, any>;
  "cad:properties"?: Record<string, any>;
  "cad:boundingRooms"?: string[];
  "top:connectsTo"?: { "@id": string }[];
  "top:adjacentTo"?: { "@id": string }[];
  "top:hasArea"?: number;
  "cad:areaSquareMetres"?: number;
  "cad:handles"?: string[];
  "cad:managed"?: boolean;
}

export interface GraphDocument {
  "@graph": GraphNode[];
  "cad:revision"?: string;
  "cad:graphRevision"?: string;
}

export interface Diagnostic {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
  affected: string[];
}

export interface CadPlanEntity {
  handle: string;
  object_type: string;
  layer: string;
  geometry: Record<string, any>;
}

export interface BackendMetadata {
  process_id: number;
  started_at: number;
  studio_version: string;
  build_id: string;
  workspace_schema_version: number;
}

export interface RuntimeState extends BackendMetadata {
  role: string;
  drawing: string;
  drawing_revision: string;
  graph_revision: string;
  updated_at: number;
}

export interface HealthResponse extends BackendMetadata {
  status: "ok";
  version: string;
  drawing?: string;
  drawing_revision?: string;
  graph_revision?: string;
  active_mcp?: RuntimeState | null;
}

export interface EditorCommand {
  op: "upsert_node" | "patch_node" | "delete_node" | "confirm_connection" | "reject_connection";
  semantic_id: string;
  node?: GraphNode;
  patch?: Record<string, unknown>;
}

export interface EditorAction {
  action_id: string;
  description: string;
  commands: EditorCommand[];
  topology_changes: TopologyChange[];
}

export interface TopologyChange {
  op: "annotate" | "create" | "update" | "delete";
  "@id"?: string;
  "@type"?: string;
  label?: string;
  targets?: {
    candidate_id?: string;
    handles?: string[];
    layer?: string;
    grouping?: "single_object" | "individual" | "connected";
  };
  properties?: Record<string, unknown>;
  geometry?: Record<string, unknown>;
  representation?: "auto" | "native_aec" | "standard";
}

export interface WorkspaceResponse {
  success: boolean;
  drawing: string;
  drawing_revision: string;
  graph_revision: string;
  units: string;
  cad_entity_count: number;
  cad_plan_entities: CadPlanEntity[];
  drawing_extents?: {
    min_x: number;
    min_y: number;
    max_x: number;
    max_y: number;
    width: number;
    height: number;
  } | null;
  unit_diagnostics: Diagnostic[];
  topology_status: {
    graph_ready: boolean;
    pending_candidate_count: number;
    portal_discovery_count: number;
    unresolved_portal_count: number;
    readiness: Record<string, unknown>;
  };
  backend: BackendMetadata;
  active_mcp?: RuntimeState | null;
  graph: GraphDocument;
  candidates: { candidate_id: string; boundary: Point[]; clear_area_m2?: number }[];
  discoveries?: Discovery[];
  diagnostics: Diagnostic[];
  analysis_diagnostics?: Diagnostic[];
  workspace: {
    draft_dirty: boolean;
    draft_revision?: string;
    draft_frozen?: boolean;
    affected_ids?: string[];
    conflict_count: number;
    conflicts: Record<string, unknown>[];
    preview_pending?: boolean;
    pending_editor_request_count?: number;
  };
  cache_hit: boolean;
}

export interface ServerEvent {
  event_type: "cad.changed" | "workspace.applied" | "workspace.previewed" | "workspace.reset" | "document.activated" | string;
  sequence: number;
  payload: Record<string, unknown>;
  timestamp?: number;
}

export interface DraftResponse {
  success: boolean;
  drawing: string;
  base_drawing_revision: string;
  base_graph_revision: string;
  draft_revision: string;
  graph: GraphDocument;
  affected_ids: string[];
  diagnostics: Diagnostic[];
  undo_position: number;
  dirty: boolean;
  frozen: boolean;
  conflicts: Record<string, unknown>[];
  action_count: number;
  mutated: false;
}

export interface SavedDraft extends DraftResponse {
  actions: EditorAction[];
  commands: EditorCommand[];
  topology_changes: TopologyChange[];
}

export interface PreviewResponse {
  success: boolean;
  transaction_id: string;
  design_transaction_id?: string;
  graph_revision: string;
  diagnostics: Diagnostic[];
  cad_preview?: Record<string, unknown>;
  mutated: false;
}

export type EditorRequestStatus = "pending" | "claimed" | "preview_ready" | "completed" | "rejected" | "stale" | "superseded" | "withdrawn";

export interface EditorRequest {
  request_id: string;
  status: EditorRequestStatus;
  drawing_name: string;
  base_drawing_revision: string;
  draft_revision: string;
  created_at: number;
  summary: string;
  requested_action: string;
  affected_entities: Array<{
    semantic_id: string;
    label?: string;
    ontology_class?: string;
    changed_fields: string[];
  }>;
  diagnostic_summary: Record<string, number>;
  next_action?: string | null;
}
