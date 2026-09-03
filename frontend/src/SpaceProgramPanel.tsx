import React, { useMemo } from "react";
import { extractRoomMetrics, ZONE_COLORS, type RoomMetrics } from "./archUtils";
import type { GraphNode, RoomZone, StudioMode } from "./types";

interface SpaceProgramProps {
  nodes: GraphNode[];
  selectedId: string | null;
  mode: StudioMode;
  onSelect: (id: string) => void;
}

export const SpaceProgramPanel: React.FC<SpaceProgramProps> = ({
  nodes,
  selectedId,
  onSelect,
}) => {
  const roomNodes = useMemo(
    () => nodes.filter((n) => n["@type"] === "top:Room" || n["@type"] === "top:Space"),
    [nodes],
  );

  const rooms: RoomMetrics[] = useMemo(
    () => roomNodes.map((n) => extractRoomMetrics(n, nodes)),
    [roomNodes, nodes],
  );

  const totalAreaM2 = useMemo(() => rooms.reduce((sum, r) => sum + r.areaM2, 0), [rooms]);

  const zoneBreakdown = useMemo(() => {
    const map: Partial<Record<RoomZone, { count: number; area: number }>> = {};
    for (const r of rooms) {
      if (!map[r.zone]) map[r.zone] = { count: 0, area: 0 };
      map[r.zone]!.count += 1;
      map[r.zone]!.area += r.areaM2;
    }
    return map;
  }, [rooms]);

  return (
    <section className="panel space-program-panel">
      <div className="program-header">
        <h2>Area Schedule & Program</h2>
        <span className="total-area-badge">{totalAreaM2.toFixed(1)} m² Total</span>
      </div>

      {/* Program Summary Cards */}
      <div className="zone-summary-bar">
        {(Object.entries(zoneBreakdown) as [RoomZone, { count: number; area: number }][]).map(
          ([zoneKey, data]) => {
            const z = ZONE_COLORS[zoneKey] || ZONE_COLORS.unassigned;
            const pct = totalAreaM2 > 0 ? (data.area / totalAreaM2) * 100 : 0;
            return (
              <div
                key={zoneKey}
                className="zone-summary-chip"
                style={{ borderColor: z.stroke, backgroundColor: z.fill }}
                title={`${z.label}: ${data.area.toFixed(1)} m² (${pct.toFixed(0)}%)`}
              >
                <span className="zone-dot" style={{ backgroundColor: z.stroke }} />
                <span className="zone-name">{z.label.split(" ")[0]}</span>
                <span className="zone-val">{data.area.toFixed(0)}m²</span>
              </div>
            );
          },
        )}
      </div>

      {/* Room Table */}
      <div className="room-schedule-table">
        {rooms.length === 0 ? (
          <p className="panel-desc empty">No rooms detected yet. Promote room candidates above.</p>
        ) : (
          rooms.map((room) => {
            const isSelected = room.id === selectedId;
            const z = ZONE_COLORS[room.zone] || ZONE_COLORS.unassigned;
            const isLandlocked = room.connectedCount === 0;

            return (
              <div
                key={room.id}
                className={`room-row ${isSelected ? "selected" : ""} ${isLandlocked ? "landlocked" : ""}`}
                onClick={() => onSelect(room.id)}
              >
                <div className="room-row-left">
                  <span className="zone-indicator" style={{ backgroundColor: z.stroke }} />
                  <span className="room-name-label">{room.name}</span>
                </div>

                <div className="room-row-right">
                  <span className="zone-mini-tag" style={{ color: z.stroke }}>{z.label.split(" ")[0]}</span>

                  <span className="room-area-pill">{room.areaM2.toFixed(1)} m²</span>

                  {room.isEntry && (
                    <span className="entry-tag" title="Main Entrance">
                      🚪 Entry
                    </span>
                  )}

                  {isLandlocked && (
                    <span className="warning-tag" title="Landlocked: No connected doors or passages">
                      ⚠️ No Door
                    </span>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
};
