import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SpaceInspector } from "./SpaceInspector";
import type { GraphNode } from "./types";

const room: GraphNode = {
  "@id": "urn:room:bed-1", "@type": "top:Room", "rdfs:label": "Room 1", "cad:managed": true,
  "cad:geometry": { boundary: [[0, 0], [4000, 0], [4000, 3500], [0, 3500]] }, "cad:properties": {},
};

describe("SpaceInspector", () => {
  it("saves a multi-field program as exactly one architect action", async () => {
    const commit = vi.fn().mockResolvedValue(undefined);
    render(<SpaceInspector selected={room} nodes={[room]} mode="design_study" stagedCount={0} disabled={false} onCommitAction={commit} onDirtyChange={() => undefined} onSaveHandler={() => undefined} onFocusCad={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Bedroom 1" } });
    fireEvent.change(screen.getByLabelText("Space type"), { target: { value: "bedroom" } });
    fireEvent.change(screen.getByLabelText("Zone"), { target: { value: "sleeping" } });
    fireEvent.change(screen.getByLabelText("Target m²"), { target: { value: "14" } });
    fireEvent.change(screen.getByLabelText("Occupancy"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Privacy"), { target: { value: "private" } });
    fireEvent.click(screen.getByRole("button", { name: "Save to Design Study" }));
    await waitFor(() => expect(commit).toHaveBeenCalledTimes(1));
    const action = commit.mock.calls[0][0];
    expect(action.commands).toHaveLength(1);
    expect(action.topology_changes).toHaveLength(0);
    expect(action.commands[0].node["cad:properties"].spatial_program.area_targets_m2.target).toBe(14);
  });
});
