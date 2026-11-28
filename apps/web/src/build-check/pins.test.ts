/**
 * What: unit tests for the PIN sources and the leak matcher used by the bundle check.
 * Use when: `pnpm test`.
 * Inputs: inline seed texts and fixture PIN lists.
 * Output: vitest results.
 * Side effects: none.
 * Role: review PR #8 I-1: the check must neither false-fail on unrelated numbers nor pass
 *   silently when the seeds file yields no PIN.
 */
import { describe, expect, it } from "vitest";

import { collectPins, findLeaks, seedPins } from "./pins";

describe("seedPins", () => {
  it("reads PINs under common key spellings", () => {
    const text = [
      'pin: "4821"',
      "  - support_pin: '6604'",
      "    pin_code: 1937",
      "PIN: 2290",
      "pin : 5518",
      '{"pin": "3141"}',
      "account_id: QS-10423",
      "amount_cents: 9600",
    ].join("\n");
    expect(seedPins(text).sort()).toEqual(["1937", "2290", "3141", "4821", "5518", "6604"]);
  });
});

describe("collectPins", () => {
  it("uses the fixtures and the seeds when the seeds file exists", () => {
    expect(collectPins(["4821"], 'pin: "7777"').sort()).toEqual(["4821", "7777"]);
  });

  it("uses the fixtures alone when there is no seeds file", () => {
    expect(collectPins(["4821", "4821"], null)).toEqual(["4821"]);
  });

  it("fails loudly when the seeds file exists but yields no PIN", () => {
    expect(() => collectPins(["4821"], "personas:\n  - name: Natasha\n    secret: 4821x\n")).toThrow(/seeds/i);
  });

  it("fails loudly when neither source yields a PIN", () => {
    expect(() => collectPins([], null)).toThrow(/no PIN/i);
  });
});

describe("findLeaks", () => {
  it("finds a PIN bundled as a string literal in any quote style", () => {
    const files = [
      { name: "a.js", text: 'x={pin:"4821",account_id:"QS-10423"}' },
      { name: "b.js", text: "y=['6604']" },
      { name: "c.js", text: "z=`1937`" },
    ];
    expect(findLeaks(files, ["4821", "6604", "1937"])).toEqual(["a.js: 4821", "b.js: 6604", "c.js: 1937"]);
  });

  it("ignores the same digits as a bare number, a decimal or part of a longer string", () => {
    const files = [{ name: "a.js", text: 'w=4821;h=0.4821;s="48210";t="x4821";year=1937' }];
    expect(findLeaks(files, ["4821", "1937"])).toEqual([]);
  });
});
