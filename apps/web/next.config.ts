/**
 * What: Next.js config for the Quillstack web app.
 * Use when: `next build`, `next dev` and `next start` load the app.
 * Inputs: the Next.js phase; QS_MOCK_API and QS_DEV_API_ORIGIN (dev server only). No secrets;
 *   NEXT_PUBLIC_* never holds a secret (doc 07 s15.2).
 * Output: the NextConfig object.
 * Side effects: none.
 * Role: standalone output for the non-root web container; the tracing root is the repo root
 *   so packages/contracts/ts can be imported. The dev-only mock API (`*.dev.ts` route files)
 *   is enabled only for the dev server with QS_MOCK_API=1, and dev-only pages (`*.dev.tsx`,
 *   the fixture replay route) only for the dev server; neither is ever in a build. With
 *   QS_DEV_API_ORIGIN (for example the lane's Caddy edge, http://127.0.0.1:3300) the dev server
 *   proxies /api/* there, so `pnpm dev` talks to a real local agent; Caddy owns /api otherwise.
 */
import path from "node:path";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";
import type { NextConfig } from "next";

/**
 * What: the repo root (two levels above apps/web).
 * Use when: setting the tracing and Turbopack roots.
 * Inputs: none.
 * Output: an absolute path.
 * Side effects: none.
 * Role: constant.
 */
const repoRoot = path.join(__dirname, "..", "..");

/**
 * What: builds the config for a phase.
 * Use when: Next.js loads next.config.ts.
 * Inputs: phase (PHASE_DEVELOPMENT_SERVER, PHASE_PRODUCTION_BUILD, ...).
 * Output: NextConfig.
 * Side effects: reads process.env.QS_MOCK_API and process.env.QS_DEV_API_ORIGIN.
 * Role: config factory; `dev.ts` joins pageExtensions only for `pnpm dev:mock`, `dev.tsx` for
 *   any dev server; the /api rewrite only for a dev server without the mock and with an origin.
 */
export default function config(phase: string): NextConfig {
  const devServer = phase === PHASE_DEVELOPMENT_SERVER;
  const mockApi = devServer && process.env.QS_MOCK_API === "1";
  const apiOrigin = devServer && !mockApi ? (process.env.QS_DEV_API_ORIGIN ?? "").replace(/\/+$/, "") : "";
  return {
    output: "standalone",
    outputFileTracingRoot: repoRoot,
    turbopack: { root: repoRoot },
    poweredByHeader: false,
    reactStrictMode: true,
    pageExtensions: [...(mockApi ? ["dev.ts"] : []), ...(devServer ? ["dev.tsx"] : []), "tsx", "ts"],
    ...(apiOrigin !== "" ? { rewrites: async () => [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }] } : {}),
  };
}
