/**
 * What: Vitest config for checks that read the production build output.
 * Use when: `pnpm test:bundle`, after `pnpm build`.
 * Inputs: none.
 * Output: the Vitest config (node environment; only `*.check.ts` files).
 * Side effects: none.
 * Role: kept apart from `pnpm test` so unit tests never need a build.
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
    include: ["src/**/*.check.ts"],
  },
});
