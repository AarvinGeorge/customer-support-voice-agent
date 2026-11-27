/**
 * What: DEV-ONLY route /console/replay?fixture=s1|s3|s4|s5|edge|voice|verify|confirm that renders a
 *   recorded fixture conversation in the console, for screenshots and design review (verify and
 *   confirm stop S1 after its first or second turn, so the open card shows).
 * Use when: `pnpm dev` (the `.dev.tsx` extension is in pageExtensions only for the dev server;
 *   `next build` never compiles this file, so fixtures never reach the production output).
 * Inputs: the `fixture` query value (unknown values fall back to s1); `demo=1` adds the fault
 *   toggle and a sample 429 banner (CS-E-07.S1.T4 screenshots).
 * Output: the console with the replayed thread.
 * Side effects: reads one JSONL file from packages/contracts/fixtures on the server.
 * Role: dev tool for CS-E-04.S5.T1 and later console tasks.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import type { SseEvent } from "@contracts/contracts";

import { FixtureReplay } from "@/dev-mock/fixture-replay";

/**
 * What: the replayable fixtures: file name and the customer's line per turn.
 * Use when: resolving the query.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: dev data (user lines are illustrative; the fixtures hold only server events). `turns`
 *   keeps only the first n turns of the file.
 */
const SCRIPTS: Readonly<Record<string, { file: string; lines: (string | null)[]; turns?: number }>> = Object.freeze({
  s1: {
    file: "s1_chat.jsonl",
    lines: [
      "Hi, we were charged twice for our Pro plan this month. Can you refund the duplicate charge?",
      null,
      "Yes, go ahead.",
      "How long until it reaches my card?",
    ],
  },
  s4: {
    file: "s4_unsure.jsonl",
    lines: ["What's the weather like tomorrow?", "Are you HIPAA compliant?", "We need a signed BAA for HIPAA before we can renew."],
  },
  s3: { file: "s3_decline.jsonl", lines: ["We were charged $240 for extra seats on 3 November. Can I get that refunded?", null] },
  s5: { file: "s5_failures.jsonl", lines: ["Can you refund the duplicate $96 charge from 21 November?", null] },
  edge: { file: "edge_turns.jsonl", lines: ["Hi there", "Please refund the duplicate charge.", "What changed on my invoice?"] },
  voice: { file: "voice_turn.jsonl", lines: ["When will my refund arrive?"] },
  verify: {
    file: "s1_chat.jsonl",
    lines: ["Hi, we were charged twice for our Pro plan this month. Can you refund the duplicate charge?"],
    turns: 1,
  },
  confirm: {
    file: "s1_chat.jsonl",
    lines: ["Hi, we were charged twice for our Pro plan this month. Can you refund the duplicate charge?", null],
    turns: 2,
  },
});

/**
 * What: the replay page.
 * Use when: route /console/replay in dev.
 * Inputs: { searchParams } (a Promise in Next.js 16).
 * Output: JSX.
 * Side effects: reads a fixture file.
 * Role: see module.
 */
export default async function ReplayPage({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const q = await searchParams;
  const key = typeof q.fixture === "string" && q.fixture in SCRIPTS ? q.fixture : "s1";
  const script = SCRIPTS[key] as { file: string; lines: (string | null)[]; turns?: number };
  const text = readFileSync(path.join(process.cwd(), "..", "..", "packages", "contracts", "fixtures", script.file), "utf8");
  const events = text
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l) as SseEvent);
  const keep = new Set([...new Set(events.map((e) => e.turn_id))].slice(0, script.turns ?? Infinity));
  return <FixtureReplay events={events.filter((e) => keep.has(e.turn_id))} lines={script.lines} demo={q.demo === "1"} />;
}
