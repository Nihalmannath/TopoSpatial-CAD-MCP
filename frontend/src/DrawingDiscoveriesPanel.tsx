import { useState, useMemo } from "react";
import type { Discovery, GraphNode, RoomZone } from "./types";

interface Props {
  discoveries: Discovery[];
  nodes: GraphNode[];
  onConfirmDiscovery: (
    discovery: Discovery,
    customLabel?: string,
    customZone?: RoomZone,
    endpointIds?: string[],
  ) => Promise<void> | void;
  onIgnoreDiscovery: (discoveryId: string) => void;
  onConfirmAllHighConfidence: () => Promise<void> | void;
  onHoverDiscovery?: (discovery: Discovery | null) => void;
}

export function DrawingDiscoveriesPanel({
  discoveries,
  nodes,
  onConfirmDiscovery,
  onIgnoreDiscovery,
  onConfirmAllHighConfidence,
  onHoverDiscovery,
}: Props) {
  const [selectedDiscoveryId, setSelectedDiscoveryId] = useState<string | null>(null);
  const [customLabel, setCustomLabel] = useState<string>("");
  const [customZone, setCustomZone] = useState<RoomZone>("unassigned");
  const [endpointIds, setEndpointIds] = useState<string[]>([]);
  const [selectedStorey, setSelectedStorey] = useState<string>("all");
  const [selectedFilter, setSelectedFilter] = useState<string>("all");

  const pendingDiscoveries = useMemo(
    () => discoveries.filter((d) => d.status === "pending" || d.status === "ambiguous" || !d.status),
    [discoveries],
  );

  const committedRooms = useMemo(
    () => nodes.filter((n) => n["@type"] === "top:Room" || n["@type"] === "top:Space"),
    [nodes],
  );

  const isInitialRecovery = committedRooms.length === 0 && pendingDiscoveries.length > 0;

  // Extract storeys
  const storeys = useMemo(() => {
    const map = new Map<string, { id: string; name: string; count: number }>();
    for (const d of pendingDiscoveries) {
      const sId = d.storey_id || "storey:default";
      const sName = d.storey_name || "Level 1";
      const existing = map.get(sId);
      if (existing) {
        existing.count += 1;
      } else {
        map.set(sId, { id: sId, name: sName, count: 1 });
      }
    }
    return Array.from(map.values());
  }, [pendingDiscoveries]);

  // Filtered discoveries
  const filteredDiscoveries = useMemo(() => {
    return pendingDiscoveries.filter((d) => {
      if (selectedStorey !== "all" && (d.storey_id || "storey:default") !== selectedStorey) {
        return false;
      }
      if (selectedFilter === "safe" && d.classification !== "safe_to_confirm") {
        return false;
      }
      if (selectedFilter === "review" && d.classification === "safe_to_confirm") {
        return false;
      }
      if (selectedFilter === "spaces" && d.kind !== "space") {
        return false;
      }
      if (selectedFilter === "portals" && d.kind === "space") {
        return false;
      }
      return true;
    });
  }, [pendingDiscoveries, selectedStorey, selectedFilter]);

  const safeCandidatesCount = useMemo(
    () => pendingDiscoveries.filter((d) => d.classification === "safe_to_confirm" || (d.confidence >= 0.75 && (d.kind === "space" || d.endpoint_candidates?.length === 2))).length,
    [pendingDiscoveries],
  );

  const nodeLabel = (id: string) =>
    nodes.find((node) => node["@id"] === id)?.["rdfs:label"] || id;

  const handleStartConfirm = (discovery: Discovery) => {
    setSelectedDiscoveryId(discovery.discovery_id);
    setCustomLabel(discovery.suggested_label || "");
    setCustomZone((discovery.suggested_zone as RoomZone) || "unassigned");
    setEndpointIds((discovery.endpoint_candidates || []).slice(0, 2));
  };

  const handleExecuteConfirm = async (discovery: Discovery) => {
    await onConfirmDiscovery(
      discovery,
      customLabel.trim() || undefined,
      customZone,
      endpointIds,
    );
    setSelectedDiscoveryId(null);
  };

  if (pendingDiscoveries.length === 0) {
    return (
      <div className="discoveries-panel empty">
        <div className="empty-state-message">
          <span className="empty-icon">✓</span>
          <h4>No Pending Discoveries</h4>
          <p>All detected drawing boundaries and portals have been reviewed and promoted into the spatial topology graph.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="discoveries-panel">
      {isInitialRecovery && (
        <div className="recovery-guidance-banner">
          <div className="banner-icon">⚡</div>
          <div className="banner-content">
            <h5>Spatial Graph Recovery</h5>
            <p>
              This drawing contains <strong>{pendingDiscoveries.length} uncommitted room and portal candidates</strong> from physical CAD linework, but <strong>0 committed rooms</strong>. Promote these candidates to build the spatial circulation graph, wall-room associations, and door connectivity.
            </p>
          </div>
        </div>
      )}

      <div className="discoveries-header">
        <div className="title-group">
          <h4>Drawing Discoveries</h4>
          <span className="badge count-badge">{pendingDiscoveries.length} candidates</span>
        </div>
        {safeCandidatesCount > 0 && (
          <button
            type="button"
            className="confirm-all-btn"
            onClick={() => onConfirmAllHighConfidence()}
            title="Promote all verified safe candidates to formal room and portal topology"
          >
            ✓ Confirm All Safe ({safeCandidatesCount})
          </button>
        )}
      </div>

      {storeys.length > 1 && (
        <div className="storey-tabs">
          <button
            type="button"
            className={`storey-tab ${selectedStorey === "all" ? "active" : ""}`}
            onClick={() => setSelectedStorey("all")}
          >
            All Storeys ({pendingDiscoveries.length})
          </button>
          {storeys.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`storey-tab ${selectedStorey === s.id ? "active" : ""}`}
              onClick={() => setSelectedStorey(s.id)}
            >
              {s.name} ({s.count})
            </button>
          ))}
        </div>
      )}

      <div className="filter-chips">
        <button
          type="button"
          className={`filter-chip ${selectedFilter === "all" ? "active" : ""}`}
          onClick={() => setSelectedFilter("all")}
        >
          All ({pendingDiscoveries.length})
        </button>
        <button
          type="button"
          className={`filter-chip ${selectedFilter === "safe" ? "active" : ""}`}
          onClick={() => setSelectedFilter("safe")}
        >
          Safe to Confirm ({safeCandidatesCount})
        </button>
        <button
          type="button"
          className={`filter-chip ${selectedFilter === "review" ? "active" : ""}`}
          onClick={() => setSelectedFilter("review")}
        >
          Needs Review ({pendingDiscoveries.length - safeCandidatesCount})
        </button>
        <button
          type="button"
          className={`filter-chip ${selectedFilter === "spaces" ? "active" : ""}`}
          onClick={() => setSelectedFilter("spaces")}
        >
          Spaces ({pendingDiscoveries.filter((d) => d.kind === "space").length})
        </button>
        <button
          type="button"
          className={`filter-chip ${selectedFilter === "portals" ? "active" : ""}`}
          onClick={() => setSelectedFilter("portals")}
        >
          Portals ({pendingDiscoveries.filter((d) => d.kind !== "space").length})
        </button>
      </div>

      <div className="discoveries-list">
        {filteredDiscoveries.map((discovery) => {
          const isSelected = selectedDiscoveryId === discovery.discovery_id;
          const confidencePct = Math.round(discovery.confidence * 100);
          const classification = discovery.classification || "safe_to_confirm";
          const isSafe = classification === "safe_to_confirm";
          const confidenceClass = isSafe ? "high" : classification === "needs_review" ? "medium" : "low";

          return (
            <div
              key={discovery.discovery_id}
              className={`discovery-card ${isSelected ? "selected" : ""} ${confidenceClass}`}
              onMouseEnter={() => onHoverDiscovery?.(discovery)}
              onMouseLeave={() => onHoverDiscovery?.(null)}
            >
              <div className="card-top">
                <span className={`kind-tag ${discovery.kind}`}>
                  {discovery.kind === "space" ? "📐 Space Candidate" : "🚪 Portal Candidate"}
                </span>
                {discovery.storey_name && (
                  <span className="storey-tag">{discovery.storey_name}</span>
                )}
                <span className={`classification-badge ${classification}`}>
                  {isSafe ? "✓ Safe" : classification.replaceAll("_", " ")}
                </span>
                <span className={`confidence-badge ${confidenceClass}`}>{confidencePct}% Match</span>
              </div>

              <div className="card-title-row">
                <span className="candidate-name">{discovery.suggested_label || "Untagged Region"}</span>
                {discovery.suggested_zone && discovery.suggested_zone !== "unassigned" && (
                  <span className="zone-pill">{discovery.suggested_zone}</span>
                )}
              </div>

              <div className="card-details">
                <div className="detail-row">
                  <span className="label">Evidence:</span>
                  <span className="value">{discovery.evidence.join(", ") || "Closed Boundary Polygon"}</span>
                </div>
                {discovery.area_m2 && (
                  <div className="detail-row">
                    <span className="label">Clear Area:</span>
                    <span className="value">{discovery.area_m2.toFixed(1)} m²</span>
                  </div>
                )}
                {discovery.aspect_ratio && (
                  <div className="detail-row">
                    <span className="label">Aspect Ratio:</span>
                    <span className="value">{discovery.aspect_ratio.toFixed(1)} : 1</span>
                  </div>
                )}
                {discovery.clear_width_mm && (
                  <div className="detail-row">
                    <span className="label">Clear Width:</span>
                    <span className="value">{Math.round(discovery.clear_width_mm)} mm</span>
                  </div>
                )}
                {discovery.source_layers.length > 0 && (
                  <div className="detail-row">
                    <span className="label">Source Layer:</span>
                    <span className="value code">{discovery.source_layers.join(", ")}</span>
                  </div>
                )}
                {discovery.kind !== "space" && (
                  <div className="detail-row">
                    <span className="label">Connects:</span>
                    <span className="value">
                      {discovery.endpoint_candidates?.length
                        ? discovery.endpoint_candidates.map(nodeLabel).join(" ↔ ")
                        : "Unresolved — assign endpoint spaces"}
                    </span>
                  </div>
                )}
                {discovery.classification_reason && (
                  <div className="detail-row review-reason">
                    <span className="label">Note:</span>
                    <span className="value">{discovery.classification_reason}</span>
                  </div>
                )}
              </div>

              {isSelected ? (
                <div className="discovery-confirm-form">
                  <div className="form-group">
                    <label>{discovery.kind === "space" ? "Space Name:" : "Door / Opening Name:"}</label>
                    <input
                      type="text"
                      className="text-input"
                      value={customLabel}
                      placeholder="e.g. Master Bedroom"
                      onChange={(e) => setCustomLabel(e.target.value)}
                    />
                  </div>
                  <div className="form-group">
                    {discovery.kind === "space" ? (
                      <>
                        <label>Architectural Zone:</label>
                        <select
                          className="select-input"
                          value={customZone}
                          onChange={(e) => setCustomZone(e.target.value as RoomZone)}
                        >
                          <option value="living">Living & Social</option>
                          <option value="sleeping">Private & Sleeping</option>
                          <option value="service">Service & Wet</option>
                          <option value="circulation">Circulation & Hall</option>
                          <option value="office">Work & Study</option>
                          <option value="outdoor">Outdoor & Balcony</option>
                          <option value="unassigned">Unassigned</option>
                        </select>
                      </>
                    ) : (
                      <>
                        <label>Connected spaces:</label>
                        {[0, 1].map((index) => (
                          <select
                            key={index}
                            className="select-input"
                            value={endpointIds[index] || ""}
                            onChange={(event) => {
                              const next = [...endpointIds];
                              next[index] = event.target.value;
                              setEndpointIds(next);
                            }}
                          >
                            <option value="">Choose space {index + 1}</option>
                            {nodes
                              .filter((node) => node["@type"] === "top:Room" || node["@type"] === "top:Space")
                              .map((node) => (
                                <option key={node["@id"]} value={node["@id"]}>
                                  {nodeLabel(node["@id"])}
                                </option>
                              ))}
                          </select>
                        ))}
                      </>
                    )}
                  </div>
                  <div className="form-actions">
                    <button
                      type="button"
                      className="primary-btn sm"
                      onClick={() => handleExecuteConfirm(discovery)}
                      disabled={
                        discovery.kind !== "space" &&
                        (endpointIds.length !== 2 || !endpointIds[0] || !endpointIds[1] || endpointIds[0] === endpointIds[1])
                      }
                    >
                      Add to Graph
                    </button>
                    <button
                      type="button"
                      className="secondary-btn sm"
                      onClick={() => setSelectedDiscoveryId(null)}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="discovery-actions">
                  <button
                    type="button"
                    className="action-btn confirm"
                    onClick={() => handleStartConfirm(discovery)}
                  >
                    ✓ Review & Confirm
                  </button>
                  <button
                    type="button"
                    className="action-btn ignore"
                    onClick={() => onIgnoreDiscovery(discovery.discovery_id)}
                  >
                    Dismiss
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
