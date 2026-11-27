/**
 * What: Vitest config for apps/web unit tests.
 * Use when: `pnpm test` runs.
 * Inputs: none.
 * Output: the Vitest config (node environment, `@/` alias to src).
 * Side effects: none.
 * Role: test runner config; client tests inject fetch and redirect, so no DOM is needed.
 */
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": path.join(import.meta.dirname, "src") },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
