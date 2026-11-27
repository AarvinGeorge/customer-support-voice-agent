/**
 * What: tests for cn with the DESIGN.md type scale.
 * Use when: `pnpm test`.
 * Inputs: none.
 * Output: vitest results.
 * Side effects: none.
 * Role: regression test: tailwind-merge dropped text-label when a text color followed it,
 *   so every Button rendered at the browser default size.
 */
import { describe, expect, it } from "vitest";

import { cn } from "./utils";

describe("cn", () => {
  it("keeps a DESIGN.md font size next to a text color", () => {
    expect(cn("text-label text-primary-foreground")).toBe("text-label text-primary-foreground");
    expect(cn("text-body-sm", "text-ink")).toBe("text-body-sm text-ink");
    expect(cn("text-mono-sm text-muted")).toBe("text-mono-sm text-muted");
  });

  it("still resolves two sizes or two colors to the last one", () => {
    expect(cn("text-label", "text-body")).toBe("text-body");
    expect(cn("text-ink", "text-muted")).toBe("text-muted");
  });
});
