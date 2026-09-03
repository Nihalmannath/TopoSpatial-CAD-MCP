import { describe, expect, it } from "vitest";
import { spatialIntentId, spatialProgramFromNode } from "./spatial";

describe("spatial intent identity", () => {
  it("normalizes symmetric endpoints but preserves ordered sequence", async () => {
    expect(await spatialIntentId("room:a", "room:b", "near", "bidirectional"))
      .toBe(await spatialIntentId("room:b", "room:a", "near", "bidirectional"));
    expect(await spatialIntentId("room:a", "room:b", "sequence", "forward"))
      .not.toBe(await spatialIntentId("room:b", "room:a", "sequence", "forward"));
  });

  it("does not infer a program from a label", () => {
    const profile = spatialProgramFromNode({ "@id": "room:1", "@type": "top:Room", "rdfs:label": "Bedroom" });
    expect(profile.space_type).toBe("other");
    expect(profile.zone).toBe("unassigned");
  });
});
