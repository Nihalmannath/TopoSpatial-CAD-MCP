import type { GraphNode, Point, RoomZone } from "./types";

export interface RoomMetrics {
  id: string;
  name: string;
  type: string;
  zone: RoomZone;
  areaM2: number;
  perimeterM: number;
  isEntry: boolean;
  connectedCount: number;
  centroid: Point;
  boundary: Point[];
}

export const ZONE_COLORS: Record<RoomZone, { stroke: string; fill: string; badge: string; text: string; label: string }> = {
  living: {
    stroke: "#ea580c",
    fill: "rgba(234, 88, 12, 0.12)",
    badge: "#ffedd5",
    text: "#9a3412",
    label: "Living & Social",
  },
  sleeping: {
    stroke: "#4f46e5",
    fill: "rgba(79, 70, 229, 0.12)",
    badge: "#e0e7ff",
    text: "#3730a3",
    label: "Private & Sleeping",
  },
  service: {
    stroke: "#0891b2",
    fill: "rgba(8, 145, 178, 0.12)",
    badge: "#cffafe",
    text: "#155e75",
    label: "Service & Wet Areas",
  },
  circulation: {
    stroke: "#059669",
    fill: "rgba(5, 150, 105, 0.12)",
    badge: "#d1fae5",
    text: "#065f46",
    label: "Circulation & Hall",
  },
  office: {
    stroke: "#7c3aed",
    fill: "rgba(124, 58, 237, 0.12)",
    badge: "#ede9fe",
    text: "#5b21b6",
    label: "Work & Study",
  },
  outdoor: {
    stroke: "#65a30d",
    fill: "rgba(101, 163, 13, 0.12)",
    badge: "#ecfccb",
    text: "#3f6212",
    label: "Outdoor & Balcony",
  },
  unassigned: {
    stroke: "#64748b",
    fill: "rgba(100, 116, 139, 0.08)",
    badge: "#f1f5f9",
    text: "#334155",
    label: "General Room",
  },
};

/** Calculate polygon area in square meters using the Shoelace formula */
export function computePolygonAreaM2(points: Point[]): number {
  if (!points || points.length < 3) return 0;
  let areaMm2 = 0;
  for (let i = 0; i < points.length; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    areaMm2 += x1 * y2 - x2 * y1;
  }
  return Math.abs(areaMm2) / 2 / 1_000_000;
}

/** Compute perimeter in meters */
export function computePerimeterM(points: Point[]): number {
  if (!points || points.length < 2) return 0;
  let totalMm = 0;
  for (let i = 0; i < points.length; i++) {
    const [x1, y1] = points[i];
    const [x2, y2] = points[(i + 1) % points.length];
    totalMm += Math.hypot(x2 - x1, y2 - y1);
  }
  return totalMm / 1000;
}

/** Compute geometric centroid of a polygon */
export function computeCentroid(points: Point[]): Point {
  if (!points || points.length === 0) return [0, 0];
  if (points.length === 1) return points[0];
  if (points.length === 2) return [(points[0][0] + points[1][0]) / 2, (points[0][1] + points[1][1]) / 2];

  let cx = 0;
  let cy = 0;
  let signedArea = 0;

  for (let i = 0; i < points.length; i++) {
    const [x0, y0] = points[i];
    const [x1, y1] = points[(i + 1) % points.length];
    const a = x0 * y1 - x1 * y0;
    signedArea += a;
    cx += (x0 + x1) * a;
    cy += (y0 + y1) * a;
  }

  signedArea *= 0.5;
  if (Math.abs(signedArea) < 1e-4) {
    // Fallback: simple average
    const sumX = points.reduce((s, p) => s + p[0], 0);
    const sumY = points.reduce((s, p) => s + p[1], 0);
    return [sumX / points.length, sumY / points.length];
  }

  cx /= 6 * signedArea;
  cy /= 6 * signedArea;
  return [cx, cy];
}

/** Infer or extract architectural zone from room name and properties */
export function inferRoomZone(name: string, props?: Record<string, any>): RoomZone {
  if (props?.zone && props.zone in ZONE_COLORS) return props.zone as RoomZone;
  const n = (name || "").toLowerCase();
  if (n.includes("bed") || n.includes("sleep") || n.includes("master") || n.includes("guest")) return "sleeping";
  if (n.includes("liv") || n.includes("lounge") || n.includes("fam") || n.includes("dining") || n.includes("salon")) return "living";
  if (n.includes("kit") || n.includes("bath") || n.includes("toilet") || n.includes("wc") || n.includes("wash") || n.includes("util") || n.includes("laundry") || n.includes("pantry")) return "service";
  if (n.includes("corr") || n.includes("hall") || n.includes("foyer") || n.includes("entry") || n.includes("lobby") || n.includes("stair") || n.includes("aisle")) return "circulation";
  if (n.includes("off") || n.includes("stud") || n.includes("desk") || n.includes("work") || n.includes("meet") || n.includes("conf")) return "office";
  if (n.includes("balc") || n.includes("terr") || n.includes("deck") || n.includes("patio") || n.includes("porch") || n.includes("yard")) return "outdoor";
  return "unassigned";
}

/** Extract full architectural metrics for a room node */
export function extractRoomMetrics(node: GraphNode, allNodes: GraphNode[]): RoomMetrics {
  const boundary = (node["cad:geometry"]?.boundary as Point[]) || [];
  const areaFromProp = node["cad:areaSquareMetres"] || node["top:hasArea"];
  const calculatedArea = computePolygonAreaM2(boundary);
  const areaM2 = areaFromProp && areaFromProp > 0 ? areaFromProp : calculatedArea;
  const perimeterM = computePerimeterM(boundary);
  const centroid = computeCentroid(boundary);
  const name = node["rdfs:label"] || node["@id"].split(":").pop() || "Room";
  const isEntry = Boolean(node["cad:properties"]?.is_entry);
  const zone = inferRoomZone(name, node["cad:properties"]);

  // Calculate connected rooms count
  let connectedCount = 0;
  const nodeId = node["@id"];
  for (const item of allNodes) {
    if (item["@type"] === "top:Connection") {
      const g = item["cad:geometry"];
      if (g?.from_space_id === nodeId || g?.to_space_id === nodeId) {
        connectedCount++;
      }
    }
  }

  return {
    id: nodeId,
    name,
    type: node["@type"],
    zone,
    areaM2,
    perimeterM,
    isEntry,
    connectedCount,
    centroid,
    boundary,
  };
}

/** Standard door opening presets for architects */
export const DOOR_PRESETS = [
  { label: "750mm · Interior Bath/Closet", width: 750, type: "single_swing" as const },
  { label: "900mm · Universal / ADA Standard", width: 900, type: "single_swing" as const },
  { label: "1000mm · Main Entrance Single", width: 1000, type: "single_swing" as const },
  { label: "1200mm · Double Door (600+600)", width: 1200, type: "double_swing" as const },
  { label: "1500mm · Sliding Pocket Door", width: 1500, type: "sliding" as const },
  { label: "1800mm · Wide Cased Opening", width: 1800, type: "cased_opening" as const },
];
