/**
 * What: tests for the shared AudioContext level meter (mic and agent analysers) and the bar
 *   history helper.
 * Use when: `pnpm test`.
 * Inputs: none.
 * Output: Vitest results.
 * Side effects: none (a fake AudioContext; no devices).
 * Role: CS-E-05.S4.T1 Done when, first half: the two sources are measured separately.
 */
import { describe, expect, it, vi } from "vitest";

import { BAR_COUNT, createAudioLevels, pushLevel, rmsLevel } from "./levels";

/**
 * What: a fake AnalyserNode whose time-domain data is a fixed amplitude around 128.
 * Use when: the level meter reads a source.
 * Inputs: the amplitude (0..127).
 * Output: an object with fftSize and getByteTimeDomainData.
 * Side effects: none.
 * Role: mocked analyser.
 */
class FakeAnalyser {
  fftSize = 2048;
  constructor(public amp: number) {}
  /** What: fills a square wave of the amplitude. Use when: level(). Inputs: buffer. Output: none. Side effects: writes buffer. Role: fake. */
  getByteTimeDomainData(buf: Uint8Array) {
    for (let i = 0; i < buf.length; i += 1) buf[i] = 128 + (i % 2 === 0 ? this.amp : -this.amp);
  }
}

/**
 * What: a fake AudioContext that hands out analysers with the amplitude set per stream id.
 * Use when: createAudioLevels in tests.
 * Inputs: a map stream id to amplitude.
 * Output: { ctx, created, closed }.
 * Side effects: none.
 * Role: mocked shared context.
 */
function fakeContext(amps: Record<string, number>) {
  const analysers: FakeAnalyser[] = [];
  let pendingStream = "";
  const ctx = {
    state: "running",
    resume: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
    createMediaStreamSource: vi.fn((stream: { id: string }) => {
      pendingStream = stream.id;
      return { connect: vi.fn(), disconnect: vi.fn() };
    }),
    createAnalyser: vi.fn(() => {
      const a = new FakeAnalyser(amps[pendingStream] ?? 0);
      analysers.push(a);
      return a;
    }),
  };
  return { ctx, analysers };
}

const stream = (id: string) => ({ id }) as unknown as MediaStream;

describe("rmsLevel", () => {
  it("is 0 for silence and grows with amplitude, capped at 1", () => {
    expect(rmsLevel(new Uint8Array(64).fill(128))).toBe(0);
    const quiet = new Uint8Array(64).map((_, i) => (i % 2 ? 138 : 118));
    const loud = new Uint8Array(64).map((_, i) => (i % 2 ? 200 : 56));
    expect(rmsLevel(quiet)).toBeGreaterThan(0);
    expect(rmsLevel(loud)).toBeGreaterThan(rmsLevel(quiet));
    expect(rmsLevel(new Uint8Array(64).map((_, i) => (i % 2 ? 255 : 0)))).toBe(1);
  });
});

describe("createAudioLevels", () => {
  it("uses one shared AudioContext for both sources and measures them separately", () => {
    const { ctx } = fakeContext({ mic: 60, remote: 0 });
    const factory = vi.fn(() => ctx as unknown as AudioContext);
    const levels = createAudioLevels(factory);
    expect(levels.level("mic")).toBe(0);
    levels.attach("mic", stream("mic"));
    levels.attach("agent", stream("remote"));
    expect(factory).toHaveBeenCalledTimes(1);
    expect(levels.level("mic")).toBeGreaterThan(0.2);
    expect(levels.level("agent")).toBe(0);
  });

  it("close releases the context; levels read 0 afterwards", () => {
    const { ctx } = fakeContext({ mic: 60 });
    const levels = createAudioLevels(() => ctx as unknown as AudioContext);
    levels.attach("mic", stream("mic"));
    levels.close();
    expect(ctx.close).toHaveBeenCalledTimes(1);
    expect(levels.level("mic")).toBe(0);
  });

  it("a context that cannot be created leaves the levels at 0 (no throw)", () => {
    const levels = createAudioLevels(() => {
      throw new Error("no audio");
    });
    expect(() => levels.attach("mic", stream("mic"))).not.toThrow();
    expect(levels.level("mic")).toBe(0);
  });
});

describe("pushLevel", () => {
  it("keeps the last BAR_COUNT levels, newest last", () => {
    let h: number[] = [];
    for (let i = 0; i < BAR_COUNT + 5; i += 1) h = pushLevel(h, i / 100);
    expect(h).toHaveLength(BAR_COUNT);
    expect(h.at(-1)).toBeCloseTo((BAR_COUNT + 4) / 100);
  });
});
