"use client";
/**
 * What: DEV-ONLY client view that replays a recorded fixture conversation into its own turn store
 *   and renders the console shell with it.
 * Use when: the dev replay route (/console/replay, `pnpm dev` only) for screenshots and design
 *   checks without the agent.
 * Inputs: { events, lines, voiceDemo? }: fixture events in wire order, the user line per turn
 *   (null for a turn started by a card, such as verification), and an optional voice dock state
 *   to show ("listening" or "interrupted", CS-E-05.S4 screenshots; no microphone or OpenAI call).
 * Output: ConsoleShell with no session (sending is disabled).
 * Side effects: none beyond its private store.
 * Role: dev tool; only imported by src/app/console/replay/page.dev.tsx, which `next build` never
 *   compiles (next.config.ts pageExtensions).
 */
import { useState } from "react";
import type { SseEvent } from "@contracts/contracts";

import { ConsoleShell } from "@/components/console/console-shell";
import { createTurnStore } from "@/lib/turns/store";
import { VOICE_LABELS } from "@/lib/voice/machine";
import type { VoiceController } from "@/lib/voice/use-voice";

/**
 * What: a stand-in voice controller for the replay: a fixed dock state, a caption, and synthetic
 *   levels (the mic moves, the agent is quiet).
 * Use when: the replay route has ?voice=listening or ?voice=interrupted.
 * Inputs: the demo state.
 * Output: a VoiceController whose actions do nothing.
 * Side effects: none.
 * Role: dev seam for screenshots.
 */
function demoVoice(state: "listening" | "interrupted"): VoiceController {
  const text = state === "listening" ? "Actually, what about the invoice ending 0412" : "Actually, wait";
  return {
    voice: { state, label: VOICE_LABELS[state], caption: { itemId: "demo", text, final: false }, error: null, muted: false, endsAt: Date.now() + 468_000, agentLine: null },
    toggle: () => undefined,
    end: () => undefined,
    mute: () => undefined,
    stopSpeaking: () => undefined,
    levels: () => {
      const t = Date.now() / 1000;
      return { mic: Math.abs(Math.sin(t * 7) * Math.sin(t * 2.3)) * 0.9, agent: 0 };
    },
    available: true,
  };
}

/**
 * What: the FixtureReplay component.
 * Use when: see module.
 * Inputs: { events, lines }.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function FixtureReplay({ events, lines, voiceDemo }: { events: SseEvent[]; lines: (string | null)[]; voiceDemo?: "listening" | "interrupted" }) {
  const [store] = useState(() => {
    const s = createTurnStore();
    const ids = [...new Set(events.map((e) => e.turn_id))];
    ids.forEach((id, i) => {
      const first = events.find((e) => e.turn_id === id);
      const line = lines[i];
      if (line) s.getState().addUserMessage(line, first?.mode ?? "chat");
      for (const e of events) if (e.turn_id === id) s.getState().apply(e);
    });
    const last = ids.at(-1);
    const turn = last === undefined ? undefined : s.getState().conversation.turns[last];
    if (voiceDemo === "interrupted" && turn !== undefined) {
      const said = "Refunds reach your card in five to ten business days, so yours should arrive by the eleventh of December.";
      s.getState().upsertAgentVoice({ itemId: "demo-a1", text: said, heard: "Refunds reach your card in five to ten business days, so yours", interrupted: true, turnId: turn.turnId });
    }
    return s;
  });
  const [voice] = useState(() => (voiceDemo === undefined ? undefined : demoVoice(voiceDemo)));
  return <ConsoleShell sessionId={null} store={store} voice={voice} />;
}
