/**
 * What: test-only loaders for the doc 07 SSE fixtures in packages/contracts/fixtures.
 * Use when: a unit test replays s1_chat.jsonl or edge_turns.jsonl through the reducer or the stream.
 * Inputs: a fixture file name.
 * Output: parsed SseEvent lists, or the SSE wire text of a list.
 * Side effects: reads files from disk (node only; never imported by app code).
 * Role: one source of fixture data for reducer, stream and store tests.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import type { SseEvent } from "@contracts/contracts";

/**
 * What: absolute path of the shared fixtures directory.
 * Use when: loading a fixture.
 * Inputs: none.
 * Output: a path string.
 * Side effects: none.
 * Role: constant.
 */
const FIXTURE_DIR = path.join(import.meta.dirname, "..", "..", "..", "..", "packages", "contracts", "fixtures");

/**
 * What: the fixture file names the reducer must replay (doc 09 CS-E-04.S4.T2 Done when).
 * Use when: iterating "both fixtures".
 * Inputs: none.
 * Output: a readonly tuple of names.
 * Side effects: none.
 * Role: constant.
 */
export const FIXTURES = ["s1_chat.jsonl", "edge_turns.jsonl"] as const;

/**
 * What: reads one JSONL fixture into events.
 * Use when: a test needs the events of one fixture in wire order.
 * Inputs: the file name.
 * Output: SseEvent[] in file order.
 * Side effects: reads the file.
 * Role: loader.
 */
export function loadFixture(name: string): SseEvent[] {
  return readFileSync(path.join(FIXTURE_DIR, name), "utf8")
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as SseEvent);
}

/**
 * What: renders events as doc 07 s6.1 wire text (`event`, `id`, `data`, blank line).
 * Use when: feeding streamTurn a fake response body.
 * Inputs: events, and an optional keepalive comment inserted before each event.
 * Output: the SSE text.
 * Side effects: none.
 * Role: wire encoder for tests.
 */
export function toWire(events: readonly SseEvent[], keepalive = false): string {
  return events
    .map((ev) => `${keepalive ? ": keepalive\n\n" : ""}event: ${ev.type}\nid: ${ev.seq}\ndata: ${JSON.stringify(ev)}\n\n`)
    .join("");
}
