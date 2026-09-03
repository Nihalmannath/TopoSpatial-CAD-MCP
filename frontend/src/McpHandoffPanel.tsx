import { useEffect, useState } from "react";
import { listMcpHandoffs, sendDraftToMcp, withdrawMcpHandoff } from "./api";
import type { DraftResponse, EditorRequest, GraphNode } from "./types";

interface Props { drawing: string; draft: DraftResponse | null; nodes: GraphNode[]; disabled: boolean; onStatus: (message: string) => void; }

export function McpHandoffPanel({ drawing, draft, nodes, disabled, onStatus }: Props) {
  const [note, setNote] = useState("");
  const [requests, setRequests] = useState<EditorRequest[]>([]);
  const [active, setActive] = useState<EditorRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    if (!drawing) return;
    const result = await listMcpHandoffs(drawing);
    setRequests(result.requests);
    setActive((current) => current ? result.requests.find((item) => item.request_id === current.request_id) || current : result.requests[0] || null);
  };
  useEffect(() => { void refresh().catch(() => undefined); }, [drawing, draft?.draft_revision]);
  const send = async () => {
    if (!draft?.dirty) return;
    setBusy(true);
    try {
      const result = await sendDraftToMcp(drawing, draft.draft_revision, note.trim());
      setActive(result.request); await refresh(); onStatus(`MCP request ${result.request.request_id.slice(-10)} is ready`);
    } finally { setBusy(false); }
  };
  const compact = active ? {
    request_id: active.request_id, drawing_name: active.drawing_name, draft_revision: active.draft_revision,
    summary: active.summary, affected: active.affected_entities.map((item) => ({ id: item.semantic_id, label: item.label })),
    requested_next_action: active.next_action || "get_editor_request",
  } : null;
  const prompt = active ? `Open TopoSpatial editor request ${active.request_id} for ${active.drawing_name}.\nCall manage_design with action get_editor_request and inspect only the affected semantic neighborhood. Then call preview_editor_request for the stored request.\nDo not create or switch drawings. Do not apply until I approve the preview.` : "";
  const copy = async (value: string, label: string) => { await navigator.clipboard.writeText(value); onStatus(`${label} copied`); };
  return <section className="panel handoff-panel"><h2>MCP Handoff</h2>
    <p className="panel-desc">Save edits to the Design Study, then create a durable request. MCP reads the stored changes; it does not rebuild them from your prompt.</p>
    <label>Instruction for MCP (optional)<textarea maxLength={1000} value={note} onChange={(event) => setNote(event.target.value)} /></label>
    <button type="button" className="primary-action-btn" disabled={disabled || busy || !draft?.dirty} onClick={() => void send()}>{busy ? "Sending…" : "Send to MCP"}</button>
    {active && <div className="handoff-card"><div><strong>{active.status.replace("_", " ")}</strong><code>{active.request_id}</code></div><p>{active.summary}</p><div className="button-row"><button type="button" onClick={() => void copy(prompt, "MCP prompt")}>Copy prompt</button><button type="button" onClick={() => void copy(JSON.stringify(compact, null, 2), "Compact JSON")}>Copy JSON</button>{["pending", "claimed", "preview_ready"].includes(active.status) && <button type="button" className="quiet" onClick={async () => { await withdrawMcpHandoff(active.request_id); await refresh(); }}>Withdraw</button>}</div></div>}
    {requests.length > 1 && <details><summary>{requests.length} requests for this drawing</summary>{requests.map((item) => <button className="request-row" type="button" key={item.request_id} onClick={() => setActive(item)}>{item.status} · {item.summary}</button>)}</details>}
  </section>;
}
