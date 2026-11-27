/**
 * What: the voice level meter: one shared AudioContext with an AnalyserNode for the local mic and
 *   one for the remote Realtime track, read as a 0..1 loudness per source; plus the bar history
 *   helper the visualizer uses.
 * Use when: VoiceClient attaches the mic stream and the remote stream; the voice dock reads
 *   levels every frame.
 * Inputs: MediaStreams; an AudioContext factory (tests pass a fake).
 * Output: an AudioLevels (attach, level, close).
 * Side effects: creates one AudioContext on the first attach and closes it on close. The
 *   analysers are not connected to the speakers (the audio element plays the remote track).
 * Role: CS-E-05.S4.T1 shared AudioContext (doc 06 s17: two AnalyserNodes; the bars are the
 *   fallback hero and the input for the orb if CS-E-05.S4.T2 lands).
 */

/**
 * What: the two measured sources.
 * Use when: typing attach and level.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: type. "mic" is the customer, "agent" the remote Realtime audio.
 */
export type LevelSource = "mic" | "agent";

/**
 * What: bars per row in the visualizer.
 * Use when: the history length.
 * Inputs: none.
 * Output: a number.
 * Side effects: none.
 * Role: constant (Figma "Console, voice (S2)" waveform strip: 60 bars).
 */
export const BAR_COUNT = 60;

/**
 * What: the meter's interface.
 * Use when: VoiceClient holds one per call.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: interface.
 */
export interface AudioLevels {
  attach: (source: LevelSource, stream: MediaStream) => void;
  level: (source: LevelSource) => number;
  close: () => void;
}

/**
 * What: RMS loudness of 8-bit time-domain samples, scaled so speech fills most of the range.
 * Use when: reading an analyser.
 * Inputs: samples centred on 128.
 * Output: 0..1.
 * Side effects: none.
 * Role: pure helper (gain 2: normal speech RMS is about 0.1 to 0.3 of full scale).
 */
export function rmsLevel(samples: Uint8Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (const s of samples) {
    const v = (s - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / samples.length) * 2);
}

/**
 * What: appends a level to a bar history, keeping the last `size`.
 * Use when: every visualizer frame.
 * Inputs: the history, the level, the size (default BAR_COUNT).
 * Output: a new array, newest last.
 * Side effects: none.
 * Role: pure helper.
 */
export function pushLevel(history: readonly number[], level: number, size = BAR_COUNT): number[] {
  const next = [...history, level];
  return next.length > size ? next.slice(next.length - size) : next;
}

/**
 * What: the default AudioContext factory.
 * Use when: no factory is given.
 * Inputs: none.
 * Output: a new AudioContext.
 * Side effects: creates an audio context.
 * Role: browser default.
 */
const browserContext = () => new AudioContext();

/**
 * What: creates the level meter.
 * Use when: one per voice call.
 * Inputs: an AudioContext factory.
 * Output: AudioLevels.
 * Side effects: see module. A factory or node that throws leaves that source at 0 (the bars sit
 *   flat; the call still works).
 * Role: see module.
 */
export function createAudioLevels(createContext: () => AudioContext = browserContext): AudioLevels {
  let ctx: AudioContext | null = null;
  let failed = false;
  const analysers = new Map<LevelSource, { node: AnalyserNode; buf: Uint8Array<ArrayBuffer>; source: MediaStreamAudioSourceNode }>();
  return {
    attach: (source, stream) => {
      if (failed) return;
      try {
        ctx ??= createContext();
        if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);
        analysers.get(source)?.source.disconnect();
        const src = ctx.createMediaStreamSource(stream);
        const node = ctx.createAnalyser();
        node.fftSize = 512;
        src.connect(node);
        analysers.set(source, { node, buf: new Uint8Array(node.fftSize), source: src });
      } catch {
        if (ctx === null) failed = true;
      }
    },
    level: (source) => {
      const a = analysers.get(source);
      if (a === undefined) return 0;
      a.node.getByteTimeDomainData(a.buf);
      return rmsLevel(a.buf);
    },
    close: () => {
      for (const a of analysers.values()) a.source.disconnect();
      analysers.clear();
      void ctx?.close().catch(() => undefined);
      ctx = null;
    },
  };
}
