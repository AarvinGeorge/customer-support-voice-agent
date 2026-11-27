/**
 * What: Vitest config for apps/web unit tests.
 * Use when: `pnpm test` runs.
 * Inputs: none.
 * Output: the Vitest config (node environment by default, jsdom per file via a
 *   `@vitest-environment jsdom` comment; `@/` alias to src, `@contracts/` to packages/contracts/ts).
 * Side effects: none.
 * Role: test runner config; the build-output PIN check runs separately (vitest.bundle.config.mts).
 */
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.join(import.meta.dirname, "src"),
      "@contracts": path.join(import.meta.dirname, "..", "..", "packages", "contracts", "ts"),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
