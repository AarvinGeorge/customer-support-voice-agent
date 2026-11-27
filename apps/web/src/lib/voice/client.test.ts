/**
 * What: tests for VoiceClient (mic, token mint, SDP exchange, data channel, teardown, 10-minute
 *   timer) with a fake RTCPeerConnection, data channel, media stream and fetch.
 * Use when: `pnpm test`.
 * Inputs: none.
 * Output: Vitest results.
 * Side effects: none (fake timers, no network, no devices).
 * Role: CS-E-05.S3.T3 connect and teardown rules (doc 07 s6.6, s8.1 voice session 10 minutes).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VoiceToken } from "@contracts/contracts";
import { createTurnStore } from "@/lib/turns/store";

import { createVoiceClient, type VoiceClientDeps, type VoiceSnapshot } from "./client";

const SID = "019a7c2e-5b10-7000-8000-00000000a001";

/**
 * What: a fake RTCDataChannel.
 * Use when: the client opens "oai-events".
 * Inputs: the label.
 * Output: an object with send, close and the event handlers.
 * Side effects: records sent messages.
 * Role: the mocked data channel.
 */
class FakeChannel {
  readyState: "connecting" | "open" | "closed" = "connecting";
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public label: string) {}
  /** What: records a message. Use when: the client sends. Inputs: data. Output: none. Side effects: push. Role: fake. */
  send(data: string) {
    this.sent.push(data);
  }
  /** What: closes. Use when: teardown. Inputs: none. Output: none. Side effects: readyState. Role: fake. */
  close() {
    this.readyState = "closed";
  }
  /** What: opens and fires onopen. Use when: a test connects. Inputs: none. Output: none. Side effects: handler. Role: fake. */
  open() {
    this.readyState = "open";
    this.onopen?.();
  }
  /** What: delivers a server event. Use when: a test plays Realtime. Inputs: event. Output: none. Side effects: handler. Role: fake. */
  emit(event: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

/**
 * What: a fake RTCPeerConnection.
 * Use when: the client builds the call.
 * Inputs: none.
 * Output: an object with the methods the client uses.
 * Side effects: records calls into the shared log.
 * Role: fake.
 */
class FakePeer {
  channel: FakeChannel | null = null;
  tracks: unknown[] = [];
  remote: RTCSessionDescriptionInit | null = null;
  closed = false;
  connectionState = "new";
  ontrack: ((e: { streams: unknown[] }) => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  constructor(private log: string[]) {}
  /** What: adds a track. Use when: mic. Inputs: track. Output: none. Side effects: log. Role: fake. */
  addTrack(track: unknown) {
    this.tracks.push(track);
    this.log.push("addTrack");
  }
  /** What: data channel. Use when: connect. Inputs: label. Output: FakeChannel. Side effects: log. Role: fake. */
  createDataChannel(label: string) {
    this.channel = new FakeChannel(label);
    this.log.push(`channel:${label}`);
    return this.channel;
  }
  /** What: offer. Use when: connect. Inputs: none. Output: offer. Side effects: log. Role: fake. */
  async createOffer() {
    this.log.push("offer");
    return { type: "offer" as const, sdp: "v=0 offer-sdp" };
  }
  /** What: local description. Use when: connect. Inputs: desc. Output: none. Side effects: log. Role: fake. */
  async setLocalDescription() {
    this.log.push("local");
  }
  /** What: remote description. Use when: answer. Inputs: desc. Output: none. Side effects: log. Role: fake. */
  async setRemoteDescription(desc: RTCSessionDescriptionInit) {
    this.remote = desc;
    this.log.push("remote");
  }
  /** What: close. Use when: teardown. Inputs: none. Output: none. Side effects: flag. Role: fake. */
  close() {
    this.closed = true;
  }
}

const TOKEN: VoiceToken = { client_secret: "ek_test_secret", expires_at: "2026-11-27T13:12:00Z", model: "gpt-realtime-2.1", voice_session_id: "vs-1", max_session_s: 600 };

/**
 * What: builds a client with fakes and a log of the order of operations.
 * Use when: each test.
 * Inputs: overrides for the deps and the token response.
 * Output: { client, log, peer(), track, fetchMock, states, deps }.
 * Side effects: none.
 * Role: test harness.
 */
function harness(over: Partial<VoiceClientDeps> = {}, tokenResponse?: () => Response) {
  const log: string[] = [];
  const track = { stop: vi.fn(() => log.push("track.stop")), kind: "audio", enabled: true };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
  let peer: FakePeer | null = null;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    if (u.endsWith("/voice/token")) {
      log.push("token");
      return tokenResponse?.() ?? new Response(JSON.stringify(TOKEN), { status: 200, headers: { "content-type": "application/json" } });
    }
    if (u === "https://api.openai.com/v1/realtime/calls") {
      log.push("sdp");
      void init;
      return new Response("v=0 answer-sdp", { status: 201, headers: { location: "/v1/realtime/calls/rtc_1" } });
    }
    log.push(`other:${u}`);
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  });
  const audio = { srcObject: null as unknown, autoplay: false, play: vi.fn(async () => {}), pause: vi.fn() };
  const attached: string[] = [];
  const levels = {
    attach: vi.fn((src: "mic" | "agent") => attached.push(src)),
    level: vi.fn((src: "mic" | "agent") => (src === "mic" ? 0.4 : 0.7)),
    close: vi.fn(),
  };
  const deps: VoiceClientDeps = {
    getUserMedia: vi.fn(async () => {
      log.push("getUserMedia");
      return stream;
    }),
    createPeer: () => {
      peer = new FakePeer(log);
      return peer as unknown as RTCPeerConnection;
    },
    createAudio: () => audio as unknown as HTMLAudioElement,
    createLevels: () => levels,
    fetch: fetchMock as unknown as typeof fetch,
    ...over,
  };
  const store = createTurnStore();
  const client = createVoiceClient({ sessionId: SID, store, deps });
  const states: string[] = [];
  client.subscribe(() => {
    const s = client.getSnapshot().state;
    if (states.at(-1) !== s) states.push(s);
  });
  return { client, log, peer: () => peer as FakePeer, track, fetchMock, states, deps, audio, store, levels, attached, stream };
}

/**
 * What: connects and brings the data channel to session.created.
 * Use when: a test needs a live call.
 * Inputs: the harness.
 * Output: the fake channel.
 * Side effects: drives the client.
 * Role: helper.
 */
async function connectLive(h: ReturnType<typeof harness>): Promise<FakeChannel> {
  await h.client.connect();
  const ch = h.peer().channel as FakeChannel;
  ch.open();
  ch.emit({ type: "session.created", session: { model: "gpt-realtime-2.1" } });
  return ch;
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("VoiceClient", () => {
  it("asks for the mic synchronously inside the click, before any await", () => {
    const h = harness();
    void h.client.connect();
    expect(h.deps.getUserMedia).toHaveBeenCalledTimes(1);
    expect(h.client.getSnapshot().state).toBe("connecting");
  });

  it("connects in order: mic, token, offer, SDP POST with the minted secret, answer; listening on session.created", async () => {
    const h = harness();
    const ch = await connectLive(h);
    expect(h.log).toEqual(["getUserMedia", "token", "addTrack", "channel:oai-events", "offer", "local", "sdp", "remote"]);
    const tokenCall = h.fetchMock.mock.calls.find(([u]) => String(u).endsWith("/voice/token"));
    expect(String(tokenCall?.[0])).toBe(`/api/sessions/${SID}/voice/token`);
    expect((tokenCall?.[1] as RequestInit).method).toBe("POST");
    const sdpInit = h.fetchMock.mock.calls.find(([u]) => String(u).startsWith("https://api.openai.com"))?.[1] as RequestInit;
    const headers = new Headers(sdpInit.headers);
    expect(sdpInit.method).toBe("POST");
    expect(headers.get("authorization")).toBe("Bearer ek_test_secret");
    expect(headers.get("content-type")).toBe("application/sdp");
    expect(sdpInit.body).toBe("v=0 offer-sdp");
    expect(h.peer().remote).toEqual({ type: "answer", sdp: "v=0 answer-sdp" });
    expect(ch.label).toBe("oai-events");
    expect(h.states).toEqual(["connecting", "listening"]);
    expect(h.client.getSnapshot().label).toBe("Listening");
  });

  it("the secret never reaches the snapshot", async () => {
    const h = harness();
    await connectLive(h);
    expect(JSON.stringify(h.client.getSnapshot())).not.toContain("ek_test_secret");
  });

  it("ends the call after 10 minutes and tears everything down", async () => {
    const h = harness();
    const ch = await connectLive(h);
    await vi.advanceTimersByTimeAsync(599_000);
    expect(h.client.getSnapshot().state).toBe("listening");
    await vi.advanceTimersByTimeAsync(1_000);
    expect(h.client.getSnapshot().state).toBe("ended");
    expect(h.track.stop).toHaveBeenCalled();
    expect(h.peer().closed).toBe(true);
    expect(ch.readyState).toBe("closed");
    expect(h.audio.srcObject).toBeNull();
  });

  it("End tears down and a late SDP answer does not revive the call", async () => {
    let release: (r: Response) => void = () => {};
    const slow = vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).endsWith("/voice/token")) return new Response(JSON.stringify(TOKEN), { status: 200, headers: { "content-type": "application/json" } });
      return new Promise<Response>((r) => (release = r));
    });
    const h = harness({ fetch: slow as unknown as typeof fetch });
    const pending = h.client.connect();
    await vi.advanceTimersByTimeAsync(0);
    h.client.end();
    expect(h.client.getSnapshot().state).toBe("ended");
    release(new Response("v=0 answer-sdp", { status: 201 }));
    await pending;
    expect(h.client.getSnapshot().state).toBe("ended");
    expect(h.peer().remote).toBeNull();
    expect(h.peer().closed).toBe(true);
    expect(h.track.stop).toHaveBeenCalled();
  });

  it("a blocked mic is an error with words and no token is minted", async () => {
    const h = harness({ getUserMedia: vi.fn(async () => Promise.reject(new DOMException("denied", "NotAllowedError"))) });
    await h.client.connect();
    const snap = h.client.getSnapshot();
    expect(snap.state).toBe("error");
    expect(snap.error).toMatch(/microphone/i);
    expect(h.fetchMock).not.toHaveBeenCalled();
  });

  it("a refused mint (429 VOICE_SESSIONS) is an error with the server's words and the mic is released", async () => {
    const h = harness({}, () =>
      new Response(JSON.stringify({ error: { class: "rate_limited", code: "VOICE_SESSIONS", message: "Too many voice sessions. Try again later.", retryable: true, fault_injected: false, changed: null, details: {}, retry_after_s: 60 } }), {
        status: 429,
        headers: { "content-type": "application/json" },
      }),
    );
    await h.client.connect();
    expect(h.client.getSnapshot().state).toBe("error");
    expect(h.client.getSnapshot().error).toContain("Too many voice sessions");
    expect(h.track.stop).toHaveBeenCalled();
  });

  it("a failed peer connection is an error; the chat thread stays", async () => {
    const h = harness();
    await connectLive(h);
    h.store.getState().addUserMessage("hello", "chat");
    const peer = h.peer();
    peer.connectionState = "failed";
    peer.onconnectionstatechange?.();
    expect(h.client.getSnapshot().state).toBe("error");
    expect(peer.closed).toBe(true);
    expect(h.store.getState().thread).toHaveLength(1);
  });

  it("data channel events drive the state machine and the relay answers on the same channel", async () => {
    const h = harness();
    const ch = await connectLive(h);
    ch.emit({ type: "input_audio_buffer.speech_started", item_id: "u1" });
    ch.emit({ type: "input_audio_buffer.committed", item_id: "u1" });
    ch.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Hi" });
    ch.emit({ type: "response.created", response: { id: "r1", status: "in_progress", output: [] } });
    ch.emit({ type: "output_audio_buffer.started", response_id: "r1" });
    expect(h.client.getSnapshot().state).toBe("speaking");
    ch.emit({ type: "response.done", response: { id: "r1", status: "completed", output: [{ type: "function_call", call_id: "c1", name: "ask_support_agent", arguments: '{"question":"Hi"}' }] } });
    expect(h.client.getSnapshot().state).toBe("thinking");
    await vi.advanceTimersByTimeAsync(0);
    await h.client.settled();
    const types = ch.sent.map((s) => (JSON.parse(s) as { type: string }).type);
    expect(types).toEqual(["conversation.item.create", "response.create"]);
    expect(h.fetchMock.mock.calls.some(([u]) => String(u) === `/api/sessions/${SID}/voice/ask`)).toBe(true);
  });

  it("a stale mic grant that resolves after a newer one never leaves a mic open (review PR #46 M1)", async () => {
    const grants: Array<(s: MediaStream) => void> = [];
    const tracks = [0, 1].map(() => ({ stop: vi.fn(), kind: "audio" }));
    const streams = tracks.map((t) => ({ getTracks: () => [t], getAudioTracks: () => [t] }) as unknown as MediaStream);
    const h = harness({ getUserMedia: vi.fn(() => new Promise<MediaStream>((r) => grants.push(r))) });
    const first = h.client.connect();
    h.client.end();
    const second = h.client.connect();
    grants[1]?.(streams[1] as MediaStream);
    await vi.advanceTimersByTimeAsync(0);
    grants[0]?.(streams[0] as MediaStream);
    await first;
    await second;
    expect(tracks[0]?.stop).toHaveBeenCalled();
    expect(h.client.getSnapshot().state).toBe("connecting");
    h.client.end();
    expect(tracks[1]?.stop).toHaveBeenCalled();
    expect(h.client.getSnapshot().state).toBe("ended");
  });

  it("feeds the mic and the remote track into one level meter, reads both, and closes it at End", async () => {
    const h = harness();
    await connectLive(h);
    expect(h.attached).toEqual(["mic"]);
    const remote = { id: "remote" };
    h.peer().ontrack?.({ streams: [remote] });
    expect(h.attached).toEqual(["mic", "agent"]);
    expect(h.client.getLevels()).toEqual({ mic: 0.4, agent: 0.7 });
    h.client.end();
    expect(h.levels.close).toHaveBeenCalledTimes(1);
  });

  it("Mute disables the mic track and Unmute enables it; the snapshot says which", async () => {
    const h = harness();
    await connectLive(h);
    h.client.toggleMute();
    expect(h.track.enabled).toBe(false);
    expect(h.client.getSnapshot().muted).toBe(true);
    expect(h.client.getLevels().mic).toBe(0);
    h.client.toggleMute();
    expect(h.track.enabled).toBe(true);
    expect(h.client.getSnapshot().muted).toBe(false);
  });

  it("records when the call ends (endsAt) from the token's max_session_s", async () => {
    vi.setSystemTime(new Date("2026-11-27T12:00:00Z"));
    const h = harness();
    await connectLive(h);
    expect(h.client.getSnapshot().endsAt).toBe(Date.parse("2026-11-27T12:10:00Z"));
    h.client.end();
    expect(h.client.getSnapshot().endsAt).toBeNull();
  });

  it("Stop speaking sends output_audio_buffer.clear on the data channel and nothing else", async () => {
    const h = harness();
    const ch = await connectLive(h);
    h.client.stopSpeaking();
    expect(ch.sent.map((m) => JSON.parse(m) as { type: string })).toEqual([{ type: "output_audio_buffer.clear" }]);
  });

  it("posts finished transcript items to /transcript when the call ends, and shows what the agent says", async () => {
    const h = harness();
    const ch = await connectLive(h);
    ch.emit({ type: "conversation.item.input_audio_transcription.completed", item_id: "u1", transcript: "Hello" });
    ch.emit({ type: "response.created", response: { id: "r1", status: "in_progress", output: [] } });
    ch.emit({ type: "response.output_audio_transcript.delta", response_id: "r1", item_id: "a1", delta: "Hi there." });
    ch.emit({ type: "output_audio_buffer.started", response_id: "r1" });
    expect(h.client.getSnapshot().agentLine).toBe("Hi there.");
    ch.emit({ type: "output_audio_buffer.stopped", response_id: "r1" });
    expect(h.client.getSnapshot().agentLine).toBeNull();
    h.client.end();
    await vi.advanceTimersByTimeAsync(0);
    const call = h.fetchMock.mock.calls.find(([u]) => String(u) === `/api/sessions/${SID}/transcript`);
    expect(call).toBeDefined();
    const body = JSON.parse(String((call?.[1] as RequestInit).body)) as { voice_session_id: string; items: Array<{ realtime_item_id: string }> };
    expect(body.voice_session_id).toBe("vs-1");
    expect(body.items.map((i) => i.realtime_item_id)).toEqual(["u1"]);
  });
});
