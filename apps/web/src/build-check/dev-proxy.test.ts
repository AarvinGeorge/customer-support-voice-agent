/**
 * What: tests that the dev-only /api proxy (QS_DEV_API_ORIGIN) exists only for `next dev`.
 * Use when: `pnpm test`.
 * Inputs: next.config.ts with each phase and env.
 * Output: Vitest results.
 * Side effects: sets and restores process.env.QS_DEV_API_ORIGIN and QS_MOCK_API.
 * Role: guard so a build or `next start` never proxies /api (Caddy routes /api in Compose and on the VM).
 */
import { afterEach, describe, expect, it } from "vitest";
import { PHASE_DEVELOPMENT_SERVER, PHASE_PRODUCTION_BUILD, PHASE_PRODUCTION_SERVER } from "next/constants";

import config from "../../next.config";

afterEach(() => {
  delete process.env.QS_DEV_API_ORIGIN;
  delete process.env.QS_MOCK_API;
});

describe("dev /api proxy", () => {
  it("next dev with QS_DEV_API_ORIGIN rewrites /api/* to that origin", async () => {
    process.env.QS_DEV_API_ORIGIN = "http://127.0.0.1:3300/";
    const rewrites = await config(PHASE_DEVELOPMENT_SERVER).rewrites?.();
    expect(rewrites).toEqual([{ source: "/api/:path*", destination: "http://127.0.0.1:3300/api/:path*" }]);
  });

  it("no proxy in a build, in next start, without the variable, or with the mock API", async () => {
    process.env.QS_DEV_API_ORIGIN = "http://127.0.0.1:3300";
    expect(config(PHASE_PRODUCTION_BUILD).rewrites).toBeUndefined();
    expect(config(PHASE_PRODUCTION_SERVER).rewrites).toBeUndefined();
    process.env.QS_MOCK_API = "1";
    expect(config(PHASE_DEVELOPMENT_SERVER).rewrites).toBeUndefined();
    delete process.env.QS_MOCK_API;
    delete process.env.QS_DEV_API_ORIGIN;
    expect(config(PHASE_DEVELOPMENT_SERVER).rewrites).toBeUndefined();
  });
});
