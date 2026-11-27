"use client";
/**
 * What: DEV-ONLY client view that replays a recorded fixture conversation into its own turn store
 *   and renders the console shell with it.
 * Use when: the dev replay route (/console/replay, `pnpm dev` only) for screenshots and design
 *   checks without the agent.
 * Inputs: { events, lines }: fixture events in wire order and the user line per turn (null for a
 *   turn started by a card, such as verification).
 * Output: ConsoleShell with no session (sending is disabled). With `demo`, the drawer shows the
 *   fault toggle (as if demo_faults_enabled were true) and a sample 429 banner with a draft.
 * Side effects: none beyond its private store.
 * Role: dev tool; only imported by src/app/console/replay/page.dev.tsx, which `next build` never
 *   compiles (next.config.ts pageExtensions).
 */
import { useState } from "react";
import type { SseEvent } from "@contracts/contracts";

import { ConsoleShell } from "@/components/console/console-shell";
import { DemoControls } from "@/components/console/demo-controls";
import { createTurnStore } from "@/lib/turns/store";

/**
 * What: a GET /api/me stand-in with the demo flag on.
 * Use when: the replay route with `demo`.
 * Inputs: none.
 * Output: a resolved Me.
 * Side effects: none.
 * Role: dev seam.
 */
const demoMe = async () => ({ visitor: true as const, operator: false, demo_faults_enabled: true });

/**
 * What: the FixtureReplay component.
 * Use when: see module.
 * Inputs: { events, lines, demo? }.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function FixtureReplay({ events, lines, demo = false }: { events: SseEvent[]; lines: (string | null)[]; demo?: boolean }) {
  const [store] = useState(() => {
    const s = createTurnStore();
    const ids = [...new Set(events.map((e) => e.turn_id))];
    ids.forEach((id, i) => {
      const first = events.find((e) => e.turn_id === id);
      const line = lines[i];
      if (line) s.getState().addUserMessage(line, first?.mode ?? "chat");
      for (const e of events) if (e.turn_id === id) s.getState().apply(e);
    });
    if (demo) {
      s.setState({
        limit: {
          id: 1,
          status: 429,
          code: "SESSION_TURNS",
          message: "You're sending messages quickly.",
          retryAfterS: 42,
          untilMs: Date.now() + 42_000,
          limit: "10/minute",
          scope: "session",
          draft: "Can you also email me the receipt?",
        },
      });
    }
    return s;
  });
  return <ConsoleShell sessionId={null} store={store} demoControls={demo ? <DemoControls store={store} loadMe={demoMe} /> : undefined} />;
}
