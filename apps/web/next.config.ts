/**
 * What: Next.js config for the Quillstack web app.
 * Use when: `next build`, `next dev` and `next start` load the app.
 * Inputs: none (no secrets; NEXT_PUBLIC_* never holds a secret, doc 07 s15.2).
 * Output: the NextConfig object.
 * Side effects: none.
 * Role: standalone output for the non-root web container; the tracing root is
 *   the repo root so files under packages/contracts can be imported once the
 *   contracts package merges.
 */
import path from "node:path";
import type { NextConfig } from "next";

const repoRoot = path.join(__dirname, "..", "..");

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: repoRoot,
  turbopack: { root: repoRoot },
  poweredByHeader: false,
  reactStrictMode: true,
};

export default nextConfig;
