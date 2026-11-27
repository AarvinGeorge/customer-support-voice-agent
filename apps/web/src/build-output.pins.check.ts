/**
 * What: proves no persona PIN is baked into the production build (CS-E-04.S1.T1 Done when:
 *   "PINs are absent from the public JS bundle"). Scans .next/static (what browsers download),
 *   .next/server and the standalone app tree (node_modules excluded).
 * Use when: `pnpm build && pnpm test:bundle` (CI runs it after the build).
 * Inputs: the .next directory from `next build`; PINs from src/dev-mock/fixtures.ts plus, when
 *   present, `pin:` values in data/seeds/personas.yaml at the repo root (lane A's real seeds).
 * Output: vitest results; fails loudly if .next is missing or the matcher cannot find a PIN in
 *   a positive control.
 * Side effects: reads files only.
 * Role: guard that PINs reach the browser only from GET /api/demo/personas at runtime.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { MOCK_PINS } from "@/dev-mock/fixtures";

/**
 * What: apps/web root.
 * Use when: resolving .next and sources.
 * Inputs: none.
 * Output: an absolute path.
 * Side effects: none.
 * Role: constant.
 */
const WEB_ROOT = path.join(import.meta.dirname, "..");
/**
 * What: the build output directory.
 * Use when: scanning.
 * Inputs: none.
 * Output: an absolute path.
 * Side effects: none.
 * Role: constant.
 */
const NEXT_DIR = path.join(WEB_ROOT, ".next");
/**
 * What: lane A's persona seed file (may not exist yet on this branch).
 * Use when: collecting real seeded PINs.
 * Inputs: none.
 * Output: an absolute path.
 * Side effects: none.
 * Role: constant.
 */
const SEEDS = path.join(WEB_ROOT, "..", "..", "data", "seeds", "personas.yaml");

/**
 * What: the PINs to look for: fixtures plus seeded values.
 * Use when: building the matcher.
 * Inputs: none.
 * Output: unique 4-digit strings.
 * Side effects: reads the seeds file when present.
 * Role: helper.
 */
function pinsToFind(): string[] {
  const pins = new Set(MOCK_PINS);
  if (existsSync(SEEDS)) {
    for (const m of readFileSync(SEEDS, "utf8").matchAll(/\bpin:\s*["']?(\d{4})["']?/g)) pins.add(m[1]!);
  }
  return [...pins];
}

/**
 * What: a matcher for a PIN as a standalone token (no digit, letter, underscore or dot on either side).
 * Use when: scanning a file's text.
 * Inputs: the PIN.
 * Output: a RegExp.
 * Side effects: none.
 * Role: helper; the dot exclusion avoids hits inside decimals such as 0.4821.
 */
function pinPattern(pin: string): RegExp {
  return new RegExp(`(?<![0-9A-Za-z_.])${pin}(?![0-9A-Za-z_.])`);
}

/**
 * What: lists files under a directory, skipping node_modules and the build cache.
 * Use when: collecting files to scan.
 * Inputs: a directory.
 * Output: absolute file paths.
 * Side effects: reads directory entries.
 * Role: helper.
 */
function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === "cache") continue;
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

/**
 * What: files whose text contains any PIN.
 * Use when: the scan and the positive control.
 * Inputs: file paths, PINs.
 * Output: "file: pin" strings for each hit.
 * Side effects: reads files.
 * Role: the measurement.
 */
function findPins(files: readonly string[], pins: readonly string[]): string[] {
  const hits: string[] = [];
  for (const file of files) {
    const text = readFileSync(file, "latin1");
    for (const pin of pins) if (pinPattern(pin).test(text)) hits.push(`${path.relative(WEB_ROOT, file)}: ${pin}`);
  }
  return hits;
}

describe("production build output", () => {
  const pins = pinsToFind();

  it("exists (run `pnpm build` first; a missing build is a failure, not a skip)", () => {
    expect(existsSync(path.join(NEXT_DIR, "static")), `${NEXT_DIR}/static is missing`).toBe(true);
    expect(existsSync(path.join(NEXT_DIR, "server")), `${NEXT_DIR}/server is missing`).toBe(true);
  });

  it("positive control: the matcher finds every PIN in the fixture source and in minified-looking text", () => {
    const fixture = path.join(WEB_ROOT, "src", "dev-mock", "fixtures.ts");
    expect(findPins([fixture], MOCK_PINS)).toHaveLength(MOCK_PINS.length);
    for (const pin of pins) expect(pinPattern(pin).test(`{pin:"${pin}",account_id:"QS-1"}`)).toBe(true);
  });

  it("contains no persona PIN in static chunks, server output or the standalone app", () => {
    const files = walk(NEXT_DIR);
    const jsInStatic = files.filter((f) => f.includes(`${path.sep}static${path.sep}`) && f.endsWith(".js"));
    expect(jsInStatic.length, "no JS chunks found; the scan would prove nothing").toBeGreaterThan(0);
    expect(findPins(files, pins)).toEqual([]);
  });
});
