import type { DraftResponse, EditorAction, EditorCommand, EditorRequest, HealthResponse, PreviewResponse, SavedDraft, ServerEvent, TopologyChange, WorkspaceResponse } from "./types";

let token = "";

export async function openSession(): Promise<string> {
  if (token) return token;
  const response = await fetch("/api/editor/session");
  const data = await response.json();
  if (!response.ok || !data.token) throw new Error(data.detail || "Could not open editor session");
  token = data.token;
  return token;
}

async function request<T>(path: string, init?: RequestInit, retry = true): Promise<T> {
  const currentToken = await openSession();
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-TopoSpatial-Token": currentToken,
      ...(init?.headers || {}),
    },
  });
  const data = await response.json();
  if (response.status === 403 && retry && data.detail === "Invalid editor session token") {
    token = "";
    await openSession();
    return request<T>(path, init, false);
  }
  if (!response.ok || data.success === false) {
    throw new Error(data.detail || data.error?.details || data.error || "Request failed");
  }
  return data as T;
}

export function getWorkspace(expectedDrawing?: string): Promise<WorkspaceResponse> {
  const expected = expectedDrawing ? `&expected_drawing=${encodeURIComponent(expectedDrawing)}` : "";
  return request(`/api/editor/workspace?include_candidates=true${expected}`);
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch("/api/health", { cache: "no-store" });
  const data = await response.json();
  if (!response.ok || data.status !== "ok") {
    throw new Error(data.detail || "TopoSpatial dashboard health check failed");
  }
  return data as HealthResponse;
}

export function getDraft(drawing: string): Promise<{ draft: SavedDraft | null }> {
  return request(`/api/editor/draft?drawing_name=${encodeURIComponent(drawing)}`);
}

export function storeDraft(
  base: WorkspaceResponse,
  actions: EditorAction[],
  undoPosition: number,
  requiredWidthMm: number,
): Promise<DraftResponse> {
  return request("/api/editor/draft", {
    method: "POST",
    body: JSON.stringify({
      drawing_name: base.drawing,
      base_drawing_revision: base.drawing_revision,
      base_graph_revision: base.graph_revision,
      actions,
      required_width_mm: requiredWidthMm,
      undo_position: undoPosition,
    }),
  });
}

export function sendDraftToMcp(
  drawing: string,
  draftRevision: string,
  userNote = "",
): Promise<{ success: true; request: EditorRequest; mutated: false }> {
  return request("/api/editor/handoffs", {
    method: "POST",
    body: JSON.stringify({
      drawing_name: drawing,
      draft_revision: draftRevision,
      requested_action: "review_and_preview",
      user_note: userNote,
    }),
  });
}

export function listMcpHandoffs(drawing: string): Promise<{ success: true; requests: EditorRequest[] }> {
  return request(`/api/editor/handoffs?drawing_name=${encodeURIComponent(drawing)}&limit=20`);
}

export function withdrawMcpHandoff(requestId: string): Promise<{ success: true; request: EditorRequest }> {
  return request(`/api/editor/handoffs/${encodeURIComponent(requestId)}/withdraw`, { method: "POST" });
}

export function resetDraft(drawing: string): Promise<Record<string, unknown>> {
  return request(`/api/editor/draft?drawing_name=${encodeURIComponent(drawing)}`, { method: "DELETE" });
}

export function rebaseDraft(drawing: string, resolutions?: Record<string, "cad" | "editor">): Promise<DraftResponse> {
  return request("/api/editor/draft/rebase", {
    method: "POST",
    body: JSON.stringify({ drawing_name: drawing, resolutions }),
  });
}

export function previewDraft(
  base: WorkspaceResponse,
  commands: EditorCommand[],
  topologyChanges: TopologyChange[],
): Promise<PreviewResponse> {
  return request("/api/editor/preview", {
    method: "POST",
    body: JSON.stringify({
      drawing_name: base.drawing,
      base_drawing_revision: base.drawing_revision,
      base_graph_revision: base.graph_revision,
      commands,
      topology_changes: topologyChanges,
    }),
  });
}

export function applyPreview(preview: PreviewResponse, drawing: string): Promise<Record<string, any>> {
  return request("/api/editor/apply", {
    method: "POST",
    body: JSON.stringify({
      transaction_id: preview.transaction_id,
      design_transaction_id: preview.design_transaction_id,
      drawing_name: drawing,
    }),
  });
}

export function cancelPreview(preview: PreviewResponse, drawing: string): Promise<Record<string, unknown>> {
  return request("/api/editor/cancel", {
    method: "POST",
    body: JSON.stringify({
      transaction_id: preview.transaction_id,
      design_transaction_id: preview.design_transaction_id,
      drawing_name: drawing,
    }),
  });
}

export function route(
  start: string,
  end: string,
  minimumWidthMm = 0,
  drawing?: string,
  draftRevision?: string,
): Promise<Record<string, unknown>> {
  const draft = drawing && draftRevision ? `&drawing_name=${encodeURIComponent(drawing)}&draft_revision=${encodeURIComponent(draftRevision)}` : "";
  return request(`/api/editor/route?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&minimum_width_mm=${minimumWidthMm}${draft}`);
}

/**
 * Live WebSocket Event Subscription for Real-Time CAD Synchronization.
 * Streams change events from AutoCAD / backend and automatically reconnects with backoff.
 */
export function subscribeEvents(
  onEvent: (event: ServerEvent) => void,
  onStatusChange?: (status: "connected" | "connecting" | "disconnected") => void,
): () => void {
  let ws: WebSocket | null = null;
  let active = true;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let retryCount = 0;

  async function connect() {
    if (!active) return;
    try {
      onStatusChange?.("connecting");
      const currentToken = await openSession();
      if (!active) return;

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const host = window.location.host;
      const url = `${protocol}//${host}/api/editor/events?token=${encodeURIComponent(currentToken)}`;

      ws = new WebSocket(url);

      ws.onopen = () => {
        if (!active) {
          ws?.close();
          return;
        }
        retryCount = 0;
        onStatusChange?.("connected");
      };

      ws.onmessage = (msgEvent) => {
        try {
          const data = JSON.parse(msgEvent.data);
          if (data && typeof data === "object" && data.event_type) {
            onEvent(data as ServerEvent);
          }
        } catch (e) {
          // ignore malformed frame
        }
      };

      ws.onerror = () => {
        // Will trigger onclose
      };

      ws.onclose = () => {
        if (!active) return;
        // A new dashboard owner has a new token. Re-authenticate on reconnect
        // instead of retrying the previous owner's token indefinitely.
        token = "";
        onStatusChange?.("disconnected");
        const backoffMs = Math.min(1000 * Math.pow(1.5, retryCount), 10000);
        retryCount++;
        retryTimer = setTimeout(connect, backoffMs);
      };
    } catch (err) {
      if (!active) return;
      onStatusChange?.("disconnected");
      retryTimer = setTimeout(connect, 3000);
    }
  }

  void connect();

  return () => {
    active = false;
    if (retryTimer) clearTimeout(retryTimer);
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
    onStatusChange?.("disconnected");
  };
}
