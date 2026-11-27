// What: ESLint flat config for the web app (Next.js core-web-vitals plus TypeScript rules).
// Use when: `pnpm lint` and the CI web job (CS-E-01.S5.T1).
// Inputs: the apps/web sources. Output: lint findings, exit 1 on any error.
// Side effects: none. Role: the web lint gate.
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // A leading underscore marks a parameter that is required by a signature (a fetch stand-in's
    // `_url, _init`) but deliberately unused; this is the TypeScript-ESLint convention.
    rules: {
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
    },
  },
  globalIgnores([".next/**", "node_modules/**", "next-env.d.ts", "coverage/**"]),
]);
