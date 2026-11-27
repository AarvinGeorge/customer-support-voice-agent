/**
 * What: proves no persona PIN is baked into the production build (CS-E-04.S1.T1 Done when:
 *   "PINs are absent from the public JS bundle"). Scans .next/static (what browsers download),
 *   .next/server and the standalone app tree (node_modules excluded).
 * Use when: `pnpm build && pnpm test:bundle` (CI runs it after the build).
 * Inputs: the .next directory from `next build`; PINs from src/dev-mock/fixtures.ts plus, when
 *   present, data/seeds/personas.yaml at the repo root (lane A's real seeds; see build-check/pins.ts).
 * Output: vitest results; fails loudly if .next is missing, if the seeds file yields no PIN, if
 *   there is no PIN at all, or if the matcher misses a PIN in the positive control.
 * Side effects: reads files only.
 * Role: guard that PINs reach the browser only from GET /api/demo/personas at runtime.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { collectPins, findLeaks } from "@/build-check/pins";
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
 * What: reads files as {name relative to apps/web, text}.
 * Use when: handing files to findLeaks.
 * Inputs: absolute paths.
 * Output: file records.
 * Side effects: reads files.
 * Role: helper.
 */
function load(files: readonly string[]): { name: string; text: string }[] {
  return files.map((f) => ({ name: path.relative(WEB_ROOT, f), text: readFileSync(f, "latin1") }));
}

describe("production build output", () => {
  const pins = collectPins(MOCK_PINS, existsSync(SEEDS) ? readFileSync(SEEDS, "utf8") : null);

  it("exists (run `pnpm build` first; a missing build is a failure, not a skip)", () => {
    expect(existsSync(path.join(NEXT_DIR, "static")), `${NEXT_DIR}/static is missing`).toBe(true);
    expect(existsSync(path.join(NEXT_DIR, "server")), `${NEXT_DIR}/server is missing`).toBe(true);
  });

  it("positive control: the matcher finds every fixture PIN in the fixture source and every PIN in minified-looking text", () => {
    const fixture = path.join(WEB_ROOT, "src", "dev-mock", "fixtures.ts");
    expect(findLeaks(load([fixture]), MOCK_PINS)).toHaveLength(MOCK_PINS.length);
    for (const pin of pins) expect(findLeaks([{ name: "x", text: `{pin:"${pin}",account_id:"QS-1"}` }], [pin])).toHaveLength(1);
  });

  it("contains no persona PIN in static chunks, server output or the standalone app", () => {
    const files = walk(NEXT_DIR);
    const jsInStatic = files.filter((f) => f.includes(`${path.sep}static${path.sep}`) && f.endsWith(".js"));
    expect(jsInStatic.length, "no JS chunks found; the scan would prove nothing").toBeGreaterThan(0);
    expect(findLeaks(load(files), pins)).toEqual([]);
  });
});
