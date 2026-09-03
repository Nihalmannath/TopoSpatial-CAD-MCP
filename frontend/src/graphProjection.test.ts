import { describe, expect, it } from "vitest";
import { OUTSIDE_SPACE_ID, projectGraph } from "./graphProjection";
import type { GraphDocument, GraphNode } from "./types";

describe("graphProjection", () => {
  const roomA: GraphNode = {
    "@id": "urn:space:living",
    "@type": "top:Room",
    "rdfs:label": "Living Room",
    "cad:areaSquareMetres": 28.5,
    "cad:geometry": {
      boundary: [[0, 0], [6000, 0], [6000, 5000], [0, 5000]],
    },
    "cad:properties": {
      spatial_program: {
        schema_version: 1,
        space_type: "living",
        zone: "living",
        area_targets_m2: { target: 28 },
        occupancy: { design_occupancy: 4, accessible_required: true },
        privacy: "public",
      },
    },
  };

  const roomB: GraphNode = {
    "@id": "urn:space:bedroom",
    "@type": "top:Room",
    "rdfs:label": "Bedroom 1",
    "cad:areaSquareMetres": 14.2,
    "cad:geometry": {
      boundary: [[6000, 0], [10000, 0], [10000, 5000], [6000, 5000]],
    },
    "cad:properties": {
      spatial_program: {
        schema_version: 1,
        space_type: "bedroom",
        zone: "sleeping",
        area_targets_m2: { target: 14 },
        occupancy: { design_occupancy: 2, accessible_required: true },
        privacy: "private",
      },
    },
  };

  const portalConnection: GraphNode = {
    "@id": "urn:topospatial:connection:door-1",
    "@type": "top:Connection",
    "rdfs:label": "Living to Bedroom Door",
    "cad:geometry": {
      from_space_id: "urn:space:living",
      to_space_id: "urn:space:bedroom",
      clear_width_mm: 900,
      direction: "bidirectional",
      door_type: "single_swing",
      status: "confirmed",
    },
    "cad:properties": {},
  };

  const facadeConnection: GraphNode = {
    "@id": "urn:topospatial:connection:entry-door",
    "@type": "top:Connection",
    "rdfs:label": "Main Entrance",
    "cad:geometry": {
      from_space_id: "urn:space:living",
      to_space_id: OUTSIDE_SPACE_ID,
      clear_width_mm: 1000,
      direction: "bidirectional",
      status: "confirmed",
    },
    "cad:properties": {},
  };

  const spatialIntent: GraphNode = {
    "@id": "urn:intent:acoustic-sep",
    "@type": "top:SpatialIntent",
    "rdfs:label": "Living separated from Bedroom",
    "cad:properties": {
      intent_schema_version: 1,
      source_space_id: "urn:space:living",
      target_space_id: "urn:space:bedroom",
      kind: "acoustic_separation",
      priority: "should",
      direction: "bidirectional",
    },
  };

  it("projects space nodes with zone, area, and centroid", () => {
    const doc: GraphDocument = {
      "@graph": [roomA, roomB],
    };
    const projected = projectGraph(doc);
    expect(projected.nodes).toHaveLength(2);

    const living = projected.nodes.find((n) => n.id === "urn:space:living");
    expect(living).toBeDefined();
    expect(living?.label).toBe("Living Room");
    expect(living?.zone).toBe("living");
    expect(living?.areaM2).toBe(28.5);
    expect(living?.isOutside).toBe(false);
  });

  it("synthesizes outside anchor node when facade connection exists", () => {
    const doc: GraphDocument = {
      "@graph": [roomA, facadeConnection],
    };
    const projected = projectGraph(doc);
    const outsideNode = projected.nodes.find((n) => n.id === OUTSIDE_SPACE_ID);
    expect(outsideNode).toBeDefined();
    expect(outsideNode?.label).toBe("Exterior Façade");
    expect(outsideNode?.isOutside).toBe(true);
    expect(outsideNode?.zone).toBe("outdoor");
  });

  it("categorizes edges into portal_connectivity, spatial_intent, and geometric_adjacency", () => {
    const doc: GraphDocument = {
      "@graph": [roomA, roomB, portalConnection, facadeConnection, spatialIntent],
    };
    const projected = projectGraph(doc);

    const portalEdge = projected.edges.find((e) => e.id === "urn:topospatial:connection:door-1");
    expect(portalEdge).toBeDefined();
    expect(portalEdge?.category).toBe("portal_connectivity");
    expect(portalEdge?.clearWidthMm).toBe(900);
    expect(portalEdge?.isFaçade).toBe(false);

    const facadeEdge = projected.edges.find((e) => e.id === "urn:topospatial:connection:entry-door");
    expect(facadeEdge).toBeDefined();
    expect(facadeEdge?.category).toBe("portal_connectivity");
    expect(facadeEdge?.isFaçade).toBe(true);

    const intentEdge = projected.edges.find((e) => e.id === "urn:intent:acoustic-sep");
    expect(intentEdge).toBeDefined();
    expect(intentEdge?.category).toBe("spatial_intent");
  });
});
