/**
 * What: compile-only check that the generated types narrow and type-check as the web app uses them.
 * Use when: CI or locally: npx -p typescript@5.6.3 tsc --noEmit --strict ts/contracts.check.ts
 * Inputs: none.
 * Output: none (a type error fails the compile).
 * Side effects: none.
 * Role: proves "web compiles against the TS types" (doc 09 CS-E-01.S6.T1 Done when).
 */
import { LIMITS, type SseEvent, type TurnRequest } from "./contracts";

/**
 * What: returns the answer text of a turn.completed event, or null for other events.
 * Use when: compile check only. Inputs: one SseEvent. Output: string or null.
 * Side effects: none. Role: discriminated-union narrowing check.
 */
export function finalText(ev: SseEvent): string | null {
  if (ev.type === "turn.completed") return ev.final.text;
  if (ev.type === "error") return ev["class"];
  return null;
}

/**
 * What: a minimal TurnRequest.
 * Use when: compile check only.
 * Inputs: none.
 * Output: a TurnRequest.
 * Side effects: none.
 * Role: proves the request type accepts the contract's required fields.
 */
export const sample: TurnRequest = { client_turn_id: "00000000-0000-4000-8000-000000000000", text: "hi" };
/**
 * What: the chat turn deadline in milliseconds.
 * Use when: compile check only.
 * Inputs: none.
 * Output: a number.
 * Side effects: none.
 * Role: proves the generated LIMITS constants are usable as numbers.
 */
export const chatDeadlineMs: number = LIMITS.CHAT_TURN_DEADLINE_S * 1000;
