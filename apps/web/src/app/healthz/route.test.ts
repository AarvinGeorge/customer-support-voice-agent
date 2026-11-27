/**
 * What: unit test for the web healthcheck route.
 * Use when: `pnpm test`.
 * Inputs: none.
 * Output: pass or fail.
 * Side effects: none.
 * Role: pins the healthcheck body the Dockerfile and Compose rely on.
 */
import { describe, expect, it } from "vitest";

import { GET } from "./route";

describe("GET /healthz", () => {
  it("answers 200 status ok, uncached", async () => {
    const res = GET();
    expect(res.status).toBe(200);
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(await res.json()).toEqual({ status: "ok" });
  });
});
