/**
 * What: unit tests for the 429 and 503 banner copy and the countdown clock.
 * Use when: `pnpm test`.
 * Inputs: LimitNotice values.
 * Output: vitest results.
 * Side effects: none.
 * Role: CS-E-07.S1.T4 copy checks.
 */
import { describe, expect, it } from "vitest";

import { formatClock, limitWords } from "./limits";
import type { LimitNotice } from "./store";

/**
 * What: a notice with defaults.
 * Use when: each case.
 * Inputs: overrides.
 * Output: LimitNotice.
 * Side effects: none.
 * Role: test helper.
 */
const notice = (o: Partial<LimitNotice>): LimitNotice => ({
  id: 1,
  status: 429,
  code: "SESSION_TURNS",
  message: "m",
  retryAfterS: 42,
  untilMs: 1,
  limit: "10/minute",
  scope: "session",
  draft: null,
  ...o,
});

describe("limitWords", () => {
  it("SESSION_TURNS: sentence and limit caption", () => {
    expect(limitWords(notice({}))).toEqual({
      message: "You're sending messages quickly. You can send again shortly; your draft is kept.",
      caption: "Limit: 10 messages per minute in this conversation. Send unlocks when the countdown ends.",
    });
  });

  it("IP_HOURLY names the network scope", () => {
    expect(limitWords(notice({ code: "IP_HOURLY", limit: "60/hour", scope: "ip" })).caption).toBe(
      "Limit: 60 messages per hour from this network. Send unlocks when the countdown ends.",
    );
  });

  it("a 503 without a wait says try again and adds no caption", () => {
    expect(limitWords(notice({ status: 503, code: "REDIS_UNAVAILABLE", untilMs: null, retryAfterS: null, limit: null, scope: null }))).toEqual({
      message: "Support is briefly unavailable. Try again in a moment; your draft is kept.",
      caption: null,
    });
  });
});

describe("formatClock", () => {
  it("formats minutes and seconds", () => {
    expect([formatClock(42), formatClock(3), formatClock(725), formatClock(0)]).toEqual(["0:42", "0:03", "12:05", "0:00"]);
  });
});
