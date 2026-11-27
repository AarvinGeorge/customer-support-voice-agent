/**
 * What: Next.js config for the Quillstack web app.
 * Use when: `next build`, `next dev` and `next start` load the app.
 * Inputs: the Next.js phase; QS_MOCK_API (dev server only). No secrets; NEXT_PUBLIC_* never
 *   holds a secret (doc 07 s15.2).
 * Output: the NextConfig object.
 * Side effects: none.
 * Role: standalone output for the non-root web container; the tracing root is the repo root
 *   so packages/contracts/ts can be imported. The dev-only mock API (`*.dev.ts` route files)
 *   is enabled only for the dev server with QS_MOCK_API=1, never in a build.
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
 * Side effects: reads process.env.QS_MOCK_API.
 * Role: config factory; `dev.ts` joins pageExtensions only for `pnpm dev:mock`.
 */
export default function config(phase: string): NextConfig {
  const mockApi = phase === PHASE_DEVELOPMENT_SERVER && process.env.QS_MOCK_API === "1";
  return {
    output: "standalone",
    outputFileTracingRoot: repoRoot,
    turbopack: { root: repoRoot },
    poweredByHeader: false,
    reactStrictMode: true,
    pageExtensions: mockApi ? ["dev.ts", "tsx", "ts"] : ["tsx", "ts"],
  };
}
