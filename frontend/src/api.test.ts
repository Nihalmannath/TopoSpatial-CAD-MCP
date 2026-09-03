import { afterEach, expect, test, vi } from "vitest";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetModules();
});

test("event reconnect obtains the replacement dashboard token", async () => {
  vi.useFakeTimers();
  const sockets: FakeSocket[] = [];
  class FakeSocket {
    onclose: (() => void) | null = null;
    onopen: (() => void) | null = null;
    onmessage: unknown;
    onerror: unknown;
    constructor(public url: string) { sockets.push(this); }
    close() {}
  }
  let calls = 0;
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, json: async () => ({ token: ++calls === 1 ? "old-owner" : "new-owner" }),
  })));
  vi.stubGlobal("WebSocket", FakeSocket);
  const { subscribeEvents } = await import("./api");
  const stop = subscribeEvents(() => {});
  await vi.advanceTimersByTimeAsync(0);
  expect(sockets[0].url).toContain("old-owner");
  sockets[0].onclose?.();
  await vi.advanceTimersByTimeAsync(1000);
  expect(sockets[1].url).toContain("new-owner");
  stop();
});
