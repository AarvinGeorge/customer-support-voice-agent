"use client";
/**
 * What: the voice dock that replaces the input bar during a call: the state in words with its
 *   icon and the time left, the two-row bar visualizer (Agent and You), the caption box, and the
 *   Mute, Stop speaking and Back to typing (End) controls.
 * Use when: the console shell while a voice call is open.
 * Inputs: { voice, levels, step?, onMute, onStopSpeaking, onEnd }.
 * Output: JSX.
 * Side effects: the bars read the level source every animation frame (10 times a second under
 *   reduced motion); the time left ticks once a second.
 * Role: CS-E-05.S4.T1, laid out from Figma "Console, voice (S2)" (Voice panel and Voice panel
 *   states). The orb is CS-E-05.S4.T2 (time-boxed, D-031): the bars are the visual until then.
 */
import { CircleAlert, Clock, Ellipsis, Keyboard, Mic, MicOff, Pause, PhoneOff, Square, Volume2, type LucideIcon } from "lucide-react";
import { useReducedMotion } from "motion/react";
import { useEffect, useEffectEvent, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import type { VoiceSnapshot } from "@/lib/voice/client";
import { BAR_COUNT, pushLevel } from "@/lib/voice/levels";
import type { VoiceState } from "@/lib/voice/machine";
import { cn } from "@/lib/utils";

/**
 * What: reads the current loudness of the mic and the agent audio.
 * Use when: the bars animate.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: the level source (VoiceClient.getLevels in the app, a stub in tests and the replay).
 */
export type VoiceLevels = () => { mic: number; agent: number };

/**
 * What: the icon of every voice state.
 * Use when: the state label.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: icon table (Figma Voice panel states; doc 06 s17: every state has a word and an icon).
 */
const VOICE_ICONS: Readonly<Record<VoiceState, LucideIcon>> = Object.freeze({
  idle: MicOff,
  connecting: Clock,
  listening: Mic,
  thinking: Ellipsis,
  speaking: Volume2,
  interrupted: Pause,
  error: CircleAlert,
  ended: PhoneOff,
});

/**
 * What: a stable name for each state's icon.
 * Use when: the svg's data-icon attribute (tests and design review).
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy of VOICE_ICONS by name.
 */
export const VOICE_ICON_NAMES: Readonly<Record<VoiceState, string>> = Object.freeze({
  idle: "mic-off",
  connecting: "clock",
  listening: "mic",
  thinking: "ellipsis",
  speaking: "volume",
  interrupted: "pause",
  error: "alert",
  ended: "phone-off",
});

/**
 * What: the caption box words when nobody is speaking.
 * Use when: no caption and no error.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: copy table (Figma state cards).
 */
const RESTING_WORDS: Readonly<Record<VoiceState, string>> = Object.freeze({
  idle: "",
  connecting: "Getting your microphone ready.",
  listening: "Go ahead, I'm listening.",
  thinking: "Checking with the support agent.",
  speaking: "The agent is answering.",
  interrupted: "Agent audio stopped. Listening to you.",
  error: "",
  ended: "Call ended.",
});

/**
 * What: the minimum and maximum bar height in px.
 * Use when: mapping a level to a bar.
 * Inputs: none.
 * Output: numbers.
 * Side effects: none.
 * Role: constants (Figma strip: 16 px tall, 2 px flat bars).
 */
const BAR_MIN = 2;
const BAR_MAX = 16;

/**
 * What: bar height for a level.
 * Use when: each frame.
 * Inputs: a level 0..1.
 * Output: px.
 * Side effects: none.
 * Role: helper.
 */
const barHeight = (level: number) => BAR_MIN + Math.max(0, Math.min(1, level)) * (BAR_MAX - BAR_MIN);

/**
 * What: props of VoiceBars.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `schedule` and `cancel` default to requestAnimationFrame (tests step frames by hand).
 */
export interface VoiceBarsProps {
  levels: VoiceLevels;
  reducedMotion: boolean;
  schedule?: (cb: () => void) => number;
  cancel?: (id: number) => void;
}

/**
 * What: the two-row visualizer: Agent (ember) above You (ink), each a rolling history of
 *   loudness; under reduced motion a static level meter per row updated 10 times a second.
 * Use when: inside the dock.
 * Inputs: VoiceBarsProps.
 * Output: JSX.
 * Side effects: a frame loop that writes bar heights straight to the DOM (no React render per
 *   frame); every second frame adds a sample, so a row shows about the last 2 seconds.
 * Role: CS-E-05.S4.T1 bars (the fallback hero, doc 07 s14 row 12).
 */
export function VoiceBars({ levels, reducedMotion, schedule, cancel }: VoiceBarsProps) {
  const agentRef = useRef<HTMLDivElement | null>(null);
  const youRef = useRef<HTMLDivElement | null>(null);
  // The loops read the latest levels without restarting: an Effect Event (react.dev,
  // "Separating Events from Effects").
  const readLevels = useEffectEvent(() => levels());
  const scheduleFrame = useEffectEvent((cb: () => void) => (schedule ?? requestAnimationFrame)(cb));
  const cancelFrame = useEffectEvent((id: number) => (cancel ?? cancelAnimationFrame)(id));
  const [meter, setMeter] = useState({ mic: 0, agent: 0 });

  useEffect(() => {
    if (!reducedMotion) return;
    const id = setInterval(() => setMeter(readLevels()), 100);
    return () => clearInterval(id);
  }, [reducedMotion]);

  useEffect(() => {
    if (reducedMotion) return;
    let agent: number[] = [];
    let you: number[] = [];
    let frame = 0;
    let id = 0;
    let stopped = false;
    /**
     * What: writes one row's history into its bars (oldest left, newest right).
     * Use when: each sampled frame.
     * Inputs: the row element, the history.
     * Output: none.
     * Side effects: style.height on the bar elements.
     * Role: helper.
     */
    const paint = (row: HTMLDivElement | null, history: number[]) => {
      if (row === null) return;
      const bars = row.children;
      const offset = BAR_COUNT - history.length;
      for (let i = 0; i < bars.length; i += 1) {
        (bars[i] as HTMLElement).style.height = `${barHeight(i >= offset ? (history[i - offset] ?? 0) : 0)}px`;
      }
    };
    /**
     * What: one animation frame.
     * Use when: scheduled.
     * Inputs: none.
     * Output: none.
     * Side effects: samples on every second frame, paints, schedules the next frame.
     * Role: loop body.
     */
    const tick = () => {
      if (stopped) return; // a frame already queued when cleanup ran must not keep the loop alive
      frame += 1;
      if (frame % 2 === 0) {
        const lv = readLevels();
        agent = pushLevel(agent, lv.agent);
        you = pushLevel(you, lv.mic);
        paint(agentRef.current, agent);
        paint(youRef.current, you);
      }
      id = scheduleFrame(tick);
    };
    id = scheduleFrame(tick);
    return () => {
      stopped = true;
      cancelFrame(id);
    };
  }, [reducedMotion]);

  const rows: Array<{ key: "agent" | "you"; name: string; color: string; ref: typeof agentRef; value: number }> = [
    { key: "agent", name: "Agent", color: "bg-ember", ref: agentRef, value: meter.agent },
    { key: "you", name: "You", color: "bg-ink", ref: youRef, value: meter.mic },
  ];
  return (
    <div className="flex flex-col gap-1">
      {rows.map((r) => (
        <div key={r.key} className="flex items-center gap-2">
          <span className="w-11 shrink-0 text-label-sm text-muted">{r.name}</span>
          {reducedMotion ? (
            <div
              role="meter"
              aria-label={r.name}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(r.value * 100)}
              className="h-1.5 w-full max-w-[18.5rem] overflow-hidden rounded-full bg-margin"
            >
              <div className={cn("h-full rounded-full", r.color)} style={{ width: `${Math.round(r.value * 100)}%` }} />
            </div>
          ) : (
            <div ref={r.ref} data-testid={`bars-${r.key}`} aria-hidden className="flex h-4 min-w-0 items-center gap-0.5 overflow-hidden">
              {Array.from({ length: BAR_COUNT }, (_, i) => (
                <span key={i} data-bar className={cn("w-[3px] shrink-0 rounded-[1.5px]", r.color)} style={{ height: `${BAR_MIN}px` }} />
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * What: the time left of the call as m:ss, ticking once a second.
 * Use when: the state label while a call is open.
 * Inputs: { endsAt } epoch ms.
 * Output: a mono span.
 * Side effects: a one-second interval.
 * Role: helper component (Figma "voice 7:48 left").
 */
function TimeLeft({ endsAt }: { endsAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const s = Math.max(0, Math.ceil((endsAt - now) / 1000));
  return (
    <span className="font-mono text-mono-sm text-muted tabular">
      · voice {Math.floor(s / 60)}:{String(s % 60).padStart(2, "0")} left
    </span>
  );
}

/**
 * What: props of VoiceDock.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `step` is the running turn's live line (the step chip while thinking); `agentLine` is
 *   what the agent is saying now (CS-E-05.S4.T3).
 */
export interface VoiceDockProps {
  voice: VoiceSnapshot;
  levels: VoiceLevels;
  step?: string | null;
  agentLine?: string | null;
  onMute: () => void;
  onStopSpeaking: () => void;
  onEnd: () => void;
}

/**
 * What: the VoiceDock component.
 * Use when: see module.
 * Inputs: VoiceDockProps.
 * Output: a region labelled "Voice call".
 * Side effects: see module.
 * Role: see module. Only the state word sits in the polite live region, so screen readers hear
 *   each state change and not the countdown tick (PRODUCT.md: voice states carry a text label;
 *   review PR #55 I3). Mute takes focus when the dock opens (review PR #55 M4).
 */
export function VoiceDock({ voice, levels, step, agentLine, onMute, onStopSpeaking, onEnd }: VoiceDockProps) {
  const reduce = useReducedMotion() === true;
  const muteRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => muteRef.current?.focus(), []);
  const Icon = VOICE_ICONS[voice.state];
  const caption = voice.caption !== null && voice.caption.text !== "" ? voice.caption : null;
  const agentSpeaking = voice.state === "speaking" && agentLine;
  return (
    <section aria-label="Voice call" data-voice-state={voice.state} className="flex w-full flex-col gap-5 rounded-[20px] border border-rule bg-raised p-6 max-sm:p-4">
      <div className="flex min-w-0 flex-col gap-3.5">
        <p data-testid="voice-state" className="flex items-center gap-2">
          <Icon aria-hidden data-icon={VOICE_ICON_NAMES[voice.state]} className={cn("size-[18px] shrink-0", voice.state === "interrupted" ? "text-warning" : voice.state === "error" ? "text-error" : "text-ink")} />
          <span role="status" aria-live="polite" className="text-heading text-ink">
            {voice.label}
          </span>
          {voice.endsAt !== null && <TimeLeft endsAt={voice.endsAt} />}
        </p>
        <VoiceBars levels={levels} reducedMotion={reduce} />
        {voice.state === "thinking" && step ? (
          <p className="inline-flex w-fit items-center gap-2 rounded-full bg-ember-soft px-3 py-1 text-label-sm text-ink">
            <span aria-hidden className="size-1.5 rounded-full bg-ember" />
            {step}
          </p>
        ) : null}
        <div className="flex flex-col gap-0.5 rounded-md bg-margin px-3.5 py-2.5 text-muted">
          {voice.error !== null ? (
            <p className="text-body text-ink">{voice.error}</p>
          ) : caption !== null && !agentSpeaking ? (
            <>
              <p className="text-label-sm">{caption.final ? "You said" : "You, live"}</p>
              <p className="text-body italic">{caption.text}</p>
            </>
          ) : agentSpeaking ? (
            <>
              <p className="text-label-sm">Agent</p>
              <p className="text-body text-ink">{agentLine}</p>
            </>
          ) : (
            <p className="text-body">{RESTING_WORDS[voice.state]}</p>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button ref={muteRef} variant="quiet" aria-pressed={voice.muted} onClick={onMute} aria-label={voice.muted ? "Unmute" : "Mute"}>
          {voice.muted ? <Mic aria-hidden className="size-3.5" /> : <MicOff aria-hidden className="size-3.5" />}
          {voice.muted ? "Unmute" : "Mute"}
        </Button>
        <Button variant="quiet" onClick={onStopSpeaking} disabled={voice.state !== "speaking"}>
          <Square aria-hidden className="size-3.5" />
          Stop speaking
        </Button>
        <span className="min-w-0 flex-1 text-caption text-muted max-sm:hidden">Headphones give the smoothest conversation</span>
        <Button variant="ink" onClick={onEnd} title="End the call and go back to typing">
          <Keyboard aria-hidden className="size-3.5" />
          Back to typing
        </Button>
      </div>
    </section>
  );
}
