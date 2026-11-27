/**
 * What: copy for the console's 429 and 503 banner: the sentence, the limit caption and the
 *   countdown clock.
 * Use when: components/console/limit-banner.tsx renders a LimitNotice.
 * Inputs: LimitNotice values (doc 07 s9 429 body: code, retry_after_s, details.limit, details.scope).
 * Output: strings.
 * Side effects: none.
 * Role: CS-E-07.S1.T4 copy, from the Figma "Rate limit banner (our 429)" frame.
 */
import type { LimitNotice } from "./store";

/**
 * What: the banner sentence before the "your draft is kept" tail, per code.
 * Use when: limitWords.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const LEAD: Readonly<Record<string, string>> = Object.freeze({
  SESSION_TURNS: "You're sending messages quickly.",
  IP_HOURLY: "This network has sent a lot of messages this hour.",
  BUSY: "We're handling a lot of conversations right now.",
});

/**
 * What: words for a limit scope.
 * Use when: the caption.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table.
 */
const SCOPE: Readonly<Record<string, string>> = Object.freeze({ session: "in this conversation", ip: "from this network" });

/**
 * What: the banner's words.
 * Use when: rendering a notice.
 * Inputs: the notice.
 * Output: { message, caption } (caption null when there is nothing to add).
 * Side effects: none.
 * Role: copy.
 */
export function limitWords(n: LimitNotice): { message: string; caption: string | null } {
  const lead = LEAD[n.code] ?? (n.status === 429 ? "Too many messages for now." : "Support is briefly unavailable.");
  const timed = n.untilMs !== null;
  const message = `${lead} ${timed ? "You can send again shortly" : "Try again in a moment"}; your draft is kept.`;
  const parts: string[] = [];
  const m = n.limit === null ? null : /^(\d+)\/(\w+)$/.exec(n.limit);
  if (m) parts.push(`Limit: ${m[1]} messages per ${m[2]}${n.scope && SCOPE[n.scope] ? ` ${SCOPE[n.scope]}` : ""}.`);
  if (timed) parts.push("Send unlocks when the countdown ends.");
  return { message, caption: parts.length > 0 ? parts.join(" ") : null };
}

/**
 * What: whole seconds as a clock ("0:42", "12:05").
 * Use when: the banner countdown.
 * Inputs: seconds (0 or more).
 * Output: "m:ss".
 * Side effects: none.
 * Role: formatter.
 */
export function formatClock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
