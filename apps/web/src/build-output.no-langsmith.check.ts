/**
 * What: proves the production build never contains a LangSmith project URL (D-087: the web bundle must not expose the
 *   maintainer's private operator tool). Scans .next/static, .next/server and the standalone tree (node_modules excluded).
 * Use when: `pnpm build && pnpm test:bundle` (CI runs it after the build).
 * Inputs: the .next directory from `next build`.
 * Output: vitest results; fails loudly if .next is missing or has no JS, or if the matcher misses a planted URL.
 * Side effects: reads files only.
 * Role: guard that no smith.langchain.com URL is baked into a public bundle.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * What: the build output directory.
 * Use when: scanning.
 * Inputs: none.
 * Output: an absolute path.
 * Side effects: none.
 * Role: constant.
 */
const NEXT_DIR = path.join(import.meta.dirname, "..", ".next");
/**
 * What: the forbidden host.
 * Use when: matching.
 * Inputs: none.
 * Output: a RegExp.
 * Side effects: none.
 * Role: constant.
 */
const FORBIDDEN = /smith\.langchain\.com/i;

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

describe("production build output (D-087)", () => {
  it("exists and has JS chunks (a missing build is a failure, not a skip)", () => {
    expect(existsSync(path.join(NEXT_DIR, "static")), `${NEXT_DIR}/static is missing`).toBe(true);
    const js = walk(NEXT_DIR).filter((f) => f.includes(`${path.sep}static${path.sep}`) && f.endsWith(".js"));
    expect(js.length, "no JS chunks found; the scan would prove nothing").toBeGreaterThan(0);
  });

  it("positive control: the matcher finds a planted LangSmith URL", () => {
    expect(FORBIDDEN.test('{u:"https://smith.langchain.com/o/1/projects/p/2"}')).toBe(true);
  });

  it("contains no smith.langchain.com URL in static chunks, server output or the standalone app", () => {
    const hits = walk(NEXT_DIR).filter((f) => FORBIDDEN.test(readFileSync(f, "latin1")));
    expect(hits.map((f) => path.relative(NEXT_DIR, f))).toEqual([]);
  });
});
