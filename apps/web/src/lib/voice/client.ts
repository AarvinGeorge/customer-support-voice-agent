/**
 * What: VoiceClient: one OpenAI Realtime call over WebRTC. Connect inside the mic click (mic,
 *   token from POST voice/token, SDP exchange with POST /v1/realtime/calls, data channel
 *   "oai-events"), feed the relay, tear down on End, on failure or after 10 minutes.
 * Use when: the console's mic button starts or ends a voice call.
 * Inputs: { sessionId, store, deps? } (deps are browser APIs, replaced in tests).
 * Output: a VoiceClient (connect, end, subscribe, getSnapshot, settled).
 * Side effects: microphone capture; one POST voice/token (agent mints the secret, doc 07 s6.6);
 *   one POST to api.openai.com with the ephemeral secret; a peer connection, data channel and
 *   audio element; a 10-minute timer (doc 07 s8.1); POST transcript of finished items (through
 *   the relay, CS-E-05.S4.T3). The secret stays in a local variable: it is
 *   never stored, logged or put in the snapshot.
 * Role: CS-E-05.S3.T3 VoiceClient. The browser never sets instructions or tools (they are in the
 *   minted session). A dropped call returns to chat with the thread intact (06 s17).
 */
import { LIMITS, type TranscriptPost, type VoiceToken } from "@contracts/contracts";
import type { StoreApi } from "zustand";
import { ApiError, apiFetch, type ApiDeps } from "@/lib/api/client";
import type { TurnStoreState } from "@/lib/turns/store";

import { VOICE_LABELS, isLive, nextVoiceState, type VoiceEvent, type VoiceState } from "./machine";
import { createAudioLevels, type AudioLevels } from "./levels";
import { createVoiceRelay, type Caption, type VoiceRelay } from "./relay";

/**
 * What: the OpenAI Realtime WebRTC call endpoint (SPIKE_NOTE s2, D-045).
 * Use when: the SDP exchange.
 * Inputs: none.
 * Output: a URL string.
 * Side effects: none.
 * Role: constant; Caddy's CSP connect-src allows exactly this origin.
 */
export const REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls";

/**
 * What: the words shown when a call cannot start or drops.
 * Use when: the error state.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const WORDS = Object.freeze({
  micBlocked: "Microphone access is blocked. Allow the microphone for this site, or keep typing in the chat.",
  micMissing: "No microphone was found. You can keep typing in the chat.",
  micFailed: "The microphone could not start. You can keep typing in the chat.",
  connect: "Voice couldn't connect just now. You can keep typing in the chat.",
  dropped: "The voice call dropped. Your conversation is still here in the chat.",
});

/**
 * What: what the UI reads.
 * Use when: rendering the mic button, label and caption.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: immutable snapshot (a new object on every change, for useSyncExternalStore).
 */
export interface VoiceSnapshot {
  state: VoiceState;
  label: string;
  caption: Caption | null;
  error: string | null;
  /** The mic track is disabled (Mute). */
  muted: boolean;
  /** Epoch ms when the 10-minute cap ends the call; null when no call is open. */
  endsAt: number | null;
  /** What the agent is saying now (live transcript), or null. */
  agentLine: string | null;
}

/**
 * What: browser APIs the client uses.
 * Use when: tests replace them; production uses the defaults.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: test seams. `fetch` is used for the token, the SDP exchange and voice/ask.
 */
export interface VoiceClientDeps {
  getUserMedia?: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createPeer?: () => RTCPeerConnection;
  createAudio?: () => HTMLAudioElement;
  createLevels?: () => AudioLevels;
  fetch?: typeof fetch;
}

/**
 * What: options of createVoiceClient.
 * Use when: creating a client per session.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: options.
 */
export interface VoiceClientOptions {
  sessionId: string;
  store: StoreApi<TurnStoreState>;
  deps?: VoiceClientDeps;
}

/**
 * What: the client's interface.
 * Use when: the voice hook holds one.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: connect must be called directly from the click handler (getUserMedia runs before any await).
 */
export interface VoiceClient {
  connect: () => Promise<void>;
  end: () => void;
  /** Disables or enables the mic track. */
  toggleMute: () => void;
  /** Stops the agent's audio (output_audio_buffer.clear); never cancels the backend turn. */
  stopSpeaking: () => void;
  /** Current loudness 0..1 of the mic and the agent audio (0 when muted or no call). */
  getLevels: () => { mic: number; agent: number };
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => VoiceSnapshot;
  settled: () => Promise<void>;
}

/**
 * What: the words for a getUserMedia rejection.
 * Use when: the mic did not start.
 * Inputs: the rejection.
 * Output: a sentence.
 * Side effects: none.
 * Role: helper.
 */
function micWords(err: unknown): string {
  const name = typeof err === "object" && err !== null && "name" in err ? String((err as { name: unknown }).name) : "";
  if (name === "NotAllowedError" || name === "SecurityError") return WORDS.micBlocked;
  if (name === "NotFoundError" || name === "OverconstrainedError") return WORDS.micMissing;
  return WORDS.micFailed;
}

/**
 * What: the default browser dependencies.
 * Use when: no test deps are given.
 * Inputs: none.
 * Output: the deps with every field set.
 * Side effects: none until called.
 * Role: helper.
 */
function browserDeps(): Required<VoiceClientDeps> {
  return {
    getUserMedia: (c) => navigator.mediaDevices.getUserMedia(c),
    createPeer: () => new RTCPeerConnection(),
    createAudio: () => new Audio(),
    createLevels: () => createAudioLevels(),
    fetch: (...args) => fetch(...args),
  };
}

/**
 * What: creates the VoiceClient for one console session.
 * Use when: the voice hook mounts for a session.
 * Inputs: VoiceClientOptions.
 * Output: VoiceClient.
 * Side effects: none until connect.
 * Role: see module. Every connect gets an epoch; End or a failure bumps it, so a step that
 *   resolves after teardown (a late SDP answer) does nothing.
 */
export function createVoiceClient({ sessionId, store, deps = {} }: VoiceClientOptions): VoiceClient {
  const d: Required<VoiceClientDeps> = { ...browserDeps(), ...deps };
  const api: ApiDeps = { fetch: d.fetch };
  const listeners = new Set<() => void>();
  let snap: VoiceSnapshot = { state: "idle", label: VOICE_LABELS.idle, caption: null, error: null, muted: false, endsAt: null, agentLine: null };
  let epoch = 0;
  let media: MediaStream | null = null;
  let pc: RTCPeerConnection | null = null;
  let dc: RTCDataChannel | null = null;
  let audio: HTMLAudioElement | null = null;
  let relay: VoiceRelay | null = null;
  let levels: AudioLevels | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  /**
   * What: replaces the snapshot and notifies listeners.
   * Use when: any visible change.
   * Inputs: the fields to change.
   * Output: none.
   * Side effects: calls listeners.
   * Role: helper.
   */
  const update = (patch: Partial<VoiceSnapshot>) => {
    const next = { ...snap, ...patch };
    next.label = VOICE_LABELS[next.state];
    snap = next;
    for (const l of listeners) l();
  };

  /**
   * What: applies a machine event.
   * Use when: the relay or the client reports an event.
   * Inputs: the event.
   * Output: none.
   * Side effects: update when the state changes.
   * Role: helper.
   */
  const dispatch = (event: VoiceEvent) => {
    const state = nextVoiceState(snap.state, event);
    if (state !== snap.state) update({ state });
  };

  /**
   * What: releases everything the call holds.
   * Use when: End, a failure, the 10-minute timer.
   * Inputs: none.
   * Output: none.
   * Side effects: stops mic tracks, closes the channel and the peer, clears the audio element and
   *   the timer and the level meter, closes the relay (an in-flight voice/ask still finishes into
   *   the thread).
   * Role: helper; safe to call twice.
   */
  const teardown = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    relay?.close();
    if (dc !== null) {
      dc.onmessage = null;
      dc.onclose = null;
      dc.close();
    }
    if (pc !== null) {
      pc.ontrack = null;
      pc.onconnectionstatechange = null;
      pc.close();
    }
    for (const t of media?.getTracks() ?? []) t.stop();
    if (audio !== null) {
      audio.pause?.();
      audio.srcObject = null;
    }
    levels?.close();
    levels = null;
    media = null;
    pc = null;
    dc = null;
  };

  /**
   * What: ends the call in the error state.
   * Use when: a step failed or the call dropped.
   * Inputs: the words to show.
   * Output: none.
   * Side effects: teardown, epoch bump, update.
   * Role: helper.
   */
  const fail = (words: string) => {
    epoch += 1;
    teardown();
    const state = nextVoiceState(snap.state, "fail");
    update({ state, error: words, caption: null, muted: false, endsAt: null, agentLine: null });
  };

  /**
   * What: ends the call by the customer or the timer.
   * Use when: the End button, unmount, the 10-minute timer.
   * Inputs: none.
   * Output: none.
   * Side effects: teardown, epoch bump, update.
   * Role: public action.
   */
  const end = () => {
    epoch += 1;
    teardown();
    const state = nextVoiceState(snap.state, "end");
    if (state !== snap.state || snap.endsAt !== null) update({ state, caption: null, muted: false, endsAt: null, agentLine: null });
  };

  /**
   * What: starts a call.
   * Use when: the mic click (call it synchronously from the handler).
   * Inputs: none.
   * Output: a promise that settles when the SDP exchange is done or the call failed.
   * Side effects: see module.
   * Role: public action; a second click while live is ignored.
   */
  const connect = async () => {
    if (isLive(snap.state)) return;
    const run = ++epoch;
    const mic = d.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    update({ state: nextVoiceState(snap.state, "start"), error: null, caption: null });
    try {
      media = await mic;
    } catch (err) {
      if (run === epoch) fail(micWords(err));
      return;
    }
    if (run !== epoch) {
      for (const t of media.getTracks()) t.stop();
      media = null;
      return;
    }
    const meter = d.createLevels();
    levels = meter;
    meter.attach("mic", media);
    try {
      const token = await apiFetch<VoiceToken>(`/api/sessions/${encodeURIComponent(sessionId)}/voice/token`, { method: "POST", body: {} }, api);
      if (run !== epoch) return;
      const maxS = Math.min(token.max_session_s || LIMITS.VOICE_SESSION_MAX_S, LIMITS.VOICE_SESSION_MAX_S);
      timer = setTimeout(end, maxS * 1000);
      update({ endsAt: Date.now() + maxS * 1000 });

      const peer = d.createPeer();
      pc = peer;
      const out = d.createAudio();
      out.autoplay = true;
      audio = out;
      peer.ontrack = (e) => {
        out.srcObject = e.streams[0] ?? null;
        if (e.streams[0]) meter.attach("agent", e.streams[0]);
        void out.play?.()?.catch(() => undefined);
      };
      peer.onconnectionstatechange = () => {
        if (run === epoch && peer.connectionState === "failed") fail(WORDS.dropped);
      };
      for (const track of media.getAudioTracks()) peer.addTrack(track, media);
      const channel = peer.createDataChannel("oai-events");
      dc = channel;
      const r = createVoiceRelay({
        sessionId,
        voiceSessionId: token.voice_session_id,
        store,
        send: (msg) => {
          if (channel.readyState === "open") channel.send(JSON.stringify(msg));
        },
        onMachine: dispatch,
        onCaption: (caption) => update({ caption }),
        onAgentLine: (agentLine) => {
          if (agentLine !== snap.agentLine) update({ agentLine });
        },
        postTranscript: async (body: TranscriptPost) => {
          await apiFetch<void>(`/api/sessions/${encodeURIComponent(sessionId)}/transcript`, { method: "POST", body }, api);
        },
        deps: api,
      });
      relay = r;
      channel.onmessage = (e: MessageEvent) => {
        let ev: unknown;
        try {
          ev = JSON.parse(String(e.data));
        } catch {
          return;
        }
        if (typeof ev === "object" && ev !== null && typeof (ev as { type?: unknown }).type === "string") r.handle(ev as { type: string });
      };
      channel.onclose = () => {
        if (run === epoch) fail(WORDS.dropped);
      };

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      if (run !== epoch) return;
      const res = await d.fetch(REALTIME_CALLS_URL, {
        method: "POST",
        body: offer.sdp,
        headers: { Authorization: `Bearer ${token.client_secret}`, "Content-Type": "application/sdp" },
      });
      if (run !== epoch) return;
      if (!res.ok) {
        fail(WORDS.connect);
        return;
      }
      const answer = await res.text();
      if (run !== epoch) return;
      await peer.setRemoteDescription({ type: "answer", sdp: answer });
    } catch (err) {
      if (run !== epoch) return;
      fail(err instanceof ApiError ? err.body.message : WORDS.connect);
    }
  };

  /**
   * What: mutes or unmutes the mic.
   * Use when: the Mute button.
   * Inputs: none.
   * Output: none.
   * Side effects: flips enabled on every mic track (a disabled track sends silence, so Realtime
   *   hears nothing); update.
   * Role: public action; does nothing without a call.
   */
  const toggleMute = () => {
    if (media === null) return;
    const muted = !snap.muted;
    for (const t of media.getAudioTracks()) t.enabled = !muted;
    update({ muted });
  };

  return {
    connect,
    end,
    toggleMute,
    stopSpeaking: () => relay?.stopSpeaking(),
    getLevels: () => ({ mic: snap.muted || levels === null ? 0 : levels.level("mic"), agent: levels?.level("agent") ?? 0 }),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => snap,
    settled: async () => {
      await relay?.settled();
    },
  };
}
