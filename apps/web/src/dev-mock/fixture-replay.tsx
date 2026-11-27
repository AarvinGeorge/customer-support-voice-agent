"use client";
/**
 * What: DEV-ONLY client view that replays a recorded fixture conversation into its own turn store
 *   and renders the console shell with it.
 * Use when: the dev replay route (/console/replay, `pnpm dev` only) for screenshots and design
 *   checks without the agent.
 * Inputs: { events, lines }: fixture events in wire order and the user line per turn (null for a
 *   turn started by a card, such as verification).
 * Output: ConsoleShell with no session (sending is disabled).
 * Side effects: none beyond its private store.
 * Role: dev tool; only imported by src/app/console/replay/page.dev.tsx, which `next build` never
 *   compiles (next.config.ts pageExtensions).
 */
import { useState } from "react";
import type { SseEvent } from "@contracts/contracts";

import { ConsoleShell } from "@/components/console/console-shell";
import { createTurnStore } from "@/lib/turns/store";

/**
 * What: the FixtureReplay component.
 * Use when: see module.
 * Inputs: { events, lines }.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function FixtureReplay({ events, lines }: { events: SseEvent[]; lines: (string | null)[] }) {
  const [store] = useState(() => {
    const s = createTurnStore();
    const ids = [...new Set(events.map((e) => e.turn_id))];
    ids.forEach((id, i) => {
      const first = events.find((e) => e.turn_id === id);
      const line = lines[i];
      if (line) s.getState().addUserMessage(line, first?.mode ?? "chat");
      for (const e of events) if (e.turn_id === id) s.getState().apply(e);
    });
    return s;
  });
  return <ConsoleShell sessionId={null} store={store} />;
}
