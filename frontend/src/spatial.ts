import type { Direction, IntentKind, GraphNode, SpatialProgram } from "./types";

export const DEFAULT_SPATIAL_PROGRAM: SpatialProgram = {
  schema_version: 1,
  space_type: "other",
  custom_space_type: "Unassigned space",
  zone: "unassigned",
  intended_use: "",
  area_targets_m2: { minimum: null, target: null, maximum: null },
  occupancy: { design_count: null, accessible_required: false },
  privacy: "semi_public",
  environment: { daylight: "none", ventilation: "none", exterior_access: "none" },
  circulation: { is_entry: false, egress_role: "none", minimum_clear_width_mm: null },
  acoustic_separation: "none",
  architect_notes: "",
};

export function spatialProgramFromNode(node: GraphNode): SpatialProgram {
  const stored = node["cad:properties"]?.spatial_program as Partial<SpatialProgram> | undefined;
  if (!stored) return structuredClone(DEFAULT_SPATIAL_PROGRAM);
  return {
    ...structuredClone(DEFAULT_SPATIAL_PROGRAM),
    ...stored,
    area_targets_m2: { ...DEFAULT_SPATIAL_PROGRAM.area_targets_m2, ...(stored.area_targets_m2 || {}) },
    occupancy: { ...DEFAULT_SPATIAL_PROGRAM.occupancy, ...(stored.occupancy || {}) },
    environment: { ...DEFAULT_SPATIAL_PROGRAM.environment, ...(stored.environment || {}) },
    circulation: { ...DEFAULT_SPATIAL_PROGRAM.circulation, ...(stored.circulation || {}) },
  };
}

export async function spatialIntentId(
  source: string,
  target: string,
  kind: IntentKind,
  direction: Direction,
): Promise<string> {
  const symmetric = new Set<IntentKind>([
    "adjacent", "near", "separated", "visual_connection", "acoustic_separation",
  ]);
  let left = source;
  let right = target;
  if (symmetric.has(kind) || (kind === "direct_access" && direction === "bidirectional")) {
    [left, right] = [left, right].sort();
  }
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`1|${kind}|${left}|${right}`),
  );
  const hex = Array.from(new Uint8Array(digest)).map((value) => value.toString(16).padStart(2, "0")).join("");
  return `urn:topospatial:intent:${hex.slice(0, 20)}`;
}

export function displayName(node: GraphNode | undefined): string {
  return node?.["rdfs:label"] || node?.["@id"].split(":").pop() || "Space";
}
