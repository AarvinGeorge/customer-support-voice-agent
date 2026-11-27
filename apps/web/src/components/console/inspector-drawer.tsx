"use client";
/**
 * What: the observability drawer (Inspector): a right-side complementary panel on Margin with the
 *   selected turn's metric strip (Mono Large), one panel with the step tree (per-step latency,
 *   model usage, Gate 1 score against tau_low and tau_high in words and numbers with the
 *   calibration status, Gate 2 claims, rule codes and policy refs), the decisions list (identity,
 *   gates, rules, action outcome, escalation, fallback), the LangSmith link and a Demo controls slot.
 * Use when: ConsoleShell's Inspector button is on.
 * Inputs: InspectorDrawerProps.
 * Output: an <aside aria-label="Inspector">.
 * Side effects: focuses its heading on mount; Escape inside it calls onClose; Copy writes the
 *   trace id to the clipboard.
 * Role: CS-E-06.S3.T1, laid out from the Figma page "Observability drawer" (the two Figma tabs
 *   are folded into one panel; the session view and trace detail tab are designed only, D-035).
 */
import {
  BookOpen,
  Check,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  Copy,
  ExternalLink,
  FileCheck,
  LifeBuoy,
  Repeat,
  ShieldCheck,
  Wallet,
  X,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import type { ConversationState, TurnView } from "@/lib/turns/reducer";
import {
  decisionRows,
  inspectorSelection,
  inspectorSteps,
  langsmithRunUrl,
  metricStrip,
  turnHeading,
  type DecisionKey,
  type InspectorStep,
  type Metric,
} from "@/lib/turns/inspector";
import { cn } from "@/lib/utils";

import { GateMeterBar, ICONS, TONE_TEXT } from "./step-trajectory";

/**
 * What: props of InspectorDrawer.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `pickedTurnId` null follows the latest turn; `id` is referenced by the trigger's
 *   aria-controls.
 */
export interface InspectorDrawerProps {
  id: string;
  conversation: ConversationState;
  pickedTurnId: string | null;
  onPick: (turnId: string) => void;
  onClose: () => void;
  langsmithProjectUrl: string;
  langsmithProject?: string;
  demoControls?: ReactNode;
}

/**
 * What: icon per decision row.
 * Use when: the Decisions list.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: icon table (Figma "Decisions").
 */
const DECISION_ICONS: Readonly<Record<DecisionKey, LucideIcon>> = Object.freeze({
  identity: ShieldCheck,
  gate1: BookOpen,
  gate2: FileCheck,
  rule: Wallet,
  action: CheckCheck,
  escalation: LifeBuoy,
  fallback: Repeat,
});

/**
 * What: the metric strip: six numbers in Mono Large, then fallback, replica and models in Mono.
 * Use when: a finished turn is selected.
 * Inputs: { metrics }.
 * Output: a labelled list.
 * Side effects: none.
 * Role: strip layout (DESIGN.md "Observability drawer").
 */
function MetricStrip({ metrics }: { metrics: Metric[] }) {
  const big = metrics.slice(0, 6);
  // Fallback and replica share a row; the model names get a full row so they never truncate.
  const small = [...metrics.slice(6).filter((m) => m.key !== "models"), ...metrics.filter((m) => m.key === "models")];
  return (
    <ul aria-label="Turn metrics" className="grid grid-cols-3 gap-x-5 gap-y-3.5">
      {big.map((m) => (
        <li key={m.key} data-metric={m.key} className="flex min-w-0 flex-col gap-0.5">
          <span className="text-label-sm text-muted">{m.label}</span>
          <span className="flex flex-wrap items-baseline gap-x-1.5">
            <span className="font-mono text-mono-lg text-ink tabular">{m.value}</span>
            {m.note && <span className="font-mono text-mono-sm text-muted tabular">{m.note}</span>}
          </span>
        </li>
      ))}
      {small.map((m) => (
        <li key={m.key} data-metric={m.key} className={cn("flex min-w-0 flex-col gap-0.5", m.key === "models" && "col-span-3")}>
          <span className="text-label-sm text-muted">{m.label}</span>
          <span className={cn("font-mono text-mono tabular break-words", m.tone === "muted" ? "text-ink" : TONE_TEXT[m.tone])}>
            {m.value}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * What: one step of the tree: icon, title, code name, latency, then detail, model usage, gates
 *   and rule lines.
 * Use when: the step list.
 * Inputs: { step }.
 * Output: an <li>.
 * Side effects: none.
 * Role: row layout from the Figma "Trace step" frames.
 */
function StepNode({ step }: { step: InspectorStep }) {
  const Icon = ICONS[step.icon];
  const g1 = step.retrieval;
  const g2 = step.grounding;
  return (
    <li className="flex flex-col gap-1 rounded-[10px] px-3 py-2.5 hover:bg-raised">
      <div className="flex min-w-0 items-center gap-2">
        <Icon aria-hidden className="size-3.5 shrink-0 text-ink" strokeWidth={1.75} />
        <span className="min-w-0 truncate text-label text-ink">{step.title}</span>
        {step.name && <span className="shrink-0 font-mono text-mono-sm text-muted">{step.name}</span>}
        <span className="flex-1" />
        <span className={cn("shrink-0 font-mono text-mono-sm tabular", step.status === "ok" ? "text-muted" : TONE_TEXT[step.tone])}>
          {step.status !== "ok" && <span>{step.status} · </span>}
          {step.latency}
        </span>
      </div>
      <div className="flex flex-col gap-1 pl-[22px]">
        {step.detail && <p className="font-mono text-mono-sm text-muted">{step.detail}</p>}
        {step.model && <p className="font-mono text-mono-sm text-muted">{step.model}</p>}
        {g1 && (
          <div className="flex flex-col gap-1">
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-mono-sm tabular">
              <span className="text-ink">Gate 1 retrieval</span>
              <span className="text-ink">{g1.numbers}</span>
              {g1.meter && <GateMeterBar meter={g1.meter} />}
              <span className={TONE_TEXT[g1.tone]}>
                {g1.verdict} · {g1.decision}
              </span>
            </p>
            <p className="text-body-sm text-ink">{g1.words}</p>
            <p className="flex flex-wrap items-center gap-2 font-mono text-mono-sm text-muted">
              <span>
                {g1.chunks} {g1.chunks === 1 ? "chunk" : "chunks"}
                {g1.codeMatch ? " · exact code match" : ""}
              </span>
              {g1.calibration && (
                <span
                  className={cn(
                    "inline-flex items-center rounded-full border border-rule bg-raised px-2 py-px font-sans text-label-sm",
                    g1.provisional ? "text-warning" : "text-success",
                  )}
                >
                  <span className="sr-only">calibration </span>
                  {g1.calibration}
                </span>
              )}
            </p>
          </div>
        )}
        {g2 && (
          <div className="flex flex-col gap-1">
            <p className="flex flex-wrap items-center gap-x-2 font-mono text-mono-sm">
              <span className="text-ink">Gate 2 grounding</span>
              <span className={TONE_TEXT[g2.tone]}>{g2.summary}</span>
              {g2.commitment && <span className="text-warning">· makes a commitment</span>}
            </p>
            {g2.claims.length > 0 && (
              <ul aria-label="Checked claims" className="flex flex-col gap-1">
                {g2.claims.map((c, i) => (
                  <li key={i} className="flex items-start gap-1.5 text-body-sm text-ink">
                    {c.supported ? (
                      <Check aria-hidden className="mt-[3px] size-3.5 shrink-0 text-success" />
                    ) : (
                      <X aria-hidden className="mt-[3px] size-3.5 shrink-0 text-error" />
                    )}
                    <span>
                      <span className="sr-only">{c.supported ? "Supported: " : "Not supported: "}</span>
                      {c.text}
                      {c.source && <span className="ml-1.5 font-mono text-mono-sm text-muted">{c.source}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {step.rule && (
          <div className="flex flex-col gap-0.5">
            <p data-rule className="flex flex-wrap gap-x-2 font-mono text-mono-sm">
              <span className={TONE_TEXT[step.rule.tone]}>{step.rule.decision}</span>
              <span className="text-ink">{step.rule.reasonCode}</span>
            </p>
            <p className="font-mono text-mono-sm text-muted">policy {step.rule.policy}</p>
          </div>
        )}
      </div>
    </li>
  );
}

/**
 * What: the body for a selected turn: strip, step tree and decisions.
 * Use when: the drawer has a turn.
 * Inputs: { turn, number }.
 * Output: JSX.
 * Side effects: none.
 * Role: layout.
 */
function TurnPanel({ turn, number }: { turn: TurnView; number: number }) {
  const metrics = metricStrip(turn);
  const steps = inspectorSteps(turn);
  const decisions = decisionRows(turn);
  return (
    <>
      <section aria-label="Usage and latency" className="border-b border-rule px-5 py-4">
        {metrics ? (
          <MetricStrip metrics={metrics} />
        ) : (
          <p className="text-body-sm text-muted">
            {turn.phase === "failed" ? "This turn did not finish, so it has no usage numbers." : "Numbers arrive when the turn finishes."}
          </p>
        )}
      </section>
      <section aria-label="Trace" className="border-b border-rule px-2 py-3">
        <p className="px-3 pb-1.5 font-mono text-mono-sm text-muted">
          Turn {number} · {turnHeading(turn)}
        </p>
        {steps.length > 0 ? (
          <ol aria-label="Steps" className="flex flex-col">
            {steps.map((s) => (
              <StepNode key={s.id} step={s} />
            ))}
          </ol>
        ) : (
          <p className="px-3 py-2 text-body-sm text-muted">No steps in this turn.</p>
        )}
      </section>
      <section aria-labelledby="inspector-decisions" className="border-b border-rule px-5 py-4">
        <h3 id="inspector-decisions" className="pb-2.5 text-label-sm text-muted">
          Decisions
        </h3>
        <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-2.5">
          {decisions.map((d, i) => {
            const Icon = DECISION_ICONS[d.key];
            return (
              <div key={`${d.key}-${i}`} className="contents">
                <dt className="flex items-start gap-2 pt-px text-label text-ink">
                  <Icon aria-hidden className="mt-0.5 size-3.5 shrink-0" strokeWidth={1.75} />
                  {d.label}
                </dt>
                <dd className={cn("text-body-sm", d.tone === "muted" || d.tone === "success" ? "text-ink" : TONE_TEXT[d.tone])}>{d.text}</dd>
              </div>
            );
          })}
        </dl>
      </section>
    </>
  );
}

/**
 * What: the InspectorDrawer component.
 * Use when: see module.
 * Inputs: InspectorDrawerProps.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module.
 */
export function InspectorDrawer({
  id,
  conversation,
  pickedTurnId,
  onPick,
  onClose,
  langsmithProjectUrl,
  langsmithProject,
  demoControls,
}: InspectorDrawerProps) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  useEffect(() => headingRef.current?.focus(), []);

  const selection = inspectorSelection(conversation.turnOrder, pickedTurnId);
  const turn = selection ? conversation.turns[selection.turnId] : undefined;
  const runUrl = selection ? langsmithRunUrl(selection.turnId, langsmithProjectUrl) : null;

  /**
   * What: closes the drawer on Escape pressed anywhere inside it.
   * Use when: keydown inside the aside.
   * Inputs: the keyboard event.
   * Output: none.
   * Side effects: calls onClose and stops the event.
   * Role: handler.
   */
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    onClose();
  };

  /**
   * What: moves the selection by one turn.
   * Use when: the previous and next buttons.
   * Inputs: -1 or 1.
   * Output: none.
   * Side effects: calls onPick.
   * Role: handler.
   */
  const step = (by: -1 | 1) => {
    if (!selection) return;
    const next = conversation.turnOrder[selection.index + by];
    if (next !== undefined) onPick(next);
  };

  /**
   * What: copies the selected turn's trace id.
   * Use when: the Copy trace id button.
   * Inputs: none.
   * Output: none.
   * Side effects: clipboard write and a toast.
   * Role: handler.
   */
  const copy = () => {
    const traceId = turn?.traceId ?? selection?.turnId;
    if (!traceId) return;
    navigator.clipboard
      ?.writeText(traceId)
      .then(() => toast.success("Trace id copied"))
      .catch(() => toast.error("Couldn't copy the trace id"));
  };

  return (
    <aside
      id={id}
      aria-label="Inspector"
      onKeyDown={onKeyDown}
      className="fixed inset-y-0 right-0 z-30 flex w-full max-w-[460px] flex-col overflow-y-auto overscroll-contain border-l border-rule bg-margin lg:sticky lg:top-[65px] lg:z-0 lg:h-[calc(100dvh-65px)] lg:w-[460px] lg:shrink-0"
    >
      <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-rule bg-margin px-5 py-3">
        <h2 ref={headingRef} tabIndex={-1} className="text-title text-ink outline-none">
          Inspector
        </h2>
        <span className="flex-1" />
        {selection && (
          <span className="flex items-center gap-1">
            <Button variant="ghost" size="icon" className="size-8" aria-label="Previous turn" disabled={selection.index === 0} onClick={() => step(-1)}>
              <ChevronLeft aria-hidden className="size-3.5" />
            </Button>
            <span className="font-mono text-mono-sm text-ink tabular" aria-live="polite">
              Turn {selection.index + 1} of {selection.total}
            </span>
            <Button
              variant="ghost"
              size="icon"
              className="size-8"
              aria-label="Next turn"
              disabled={selection.index === selection.total - 1}
              onClick={() => step(1)}
            >
              <ChevronRight aria-hidden className="size-3.5" />
            </Button>
          </span>
        )}
        <Button variant="ghost" size="icon" className="size-8" aria-label="Close inspector" onClick={onClose}>
          <X aria-hidden className="size-4" />
        </Button>
      </header>

      <div className="flex-1">
        {selection && turn ? (
          <TurnPanel turn={turn} number={selection.index + 1} />
        ) : (
          <p className="px-5 py-6 text-body-sm text-muted">No turns yet. Send a message and each step, check and number shows here.</p>
        )}
      </div>

      <footer className="flex flex-col gap-3 px-5 py-4">
        {selection && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            {runUrl ? (
              <a
                href={runUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-sm text-label text-ink underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              >
                Open in LangSmith
                <ExternalLink aria-hidden className="size-3" />
                <span className="sr-only"> (opens in a new tab{langsmithProject ? `, project ${langsmithProject}` : ""})</span>
              </a>
            ) : (
              <span className="text-label text-muted">LangSmith link not configured</span>
            )}
            <button
              type="button"
              onClick={copy}
              aria-label="Copy trace id"
              title={turn?.traceId ?? selection.turnId}
              className="inline-flex items-center gap-1.5 rounded-sm font-mono text-mono-sm text-muted hover:text-ink focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <Copy aria-hidden className="size-3" />
              <span>trace {(turn?.traceId ?? selection.turnId).slice(-8)}</span>
            </button>
          </div>
        )}
        {demoControls && (
          <section aria-label="Demo controls" className="flex flex-col gap-2">
            {demoControls}
          </section>
        )}
      </footer>
    </aside>
  );
}
