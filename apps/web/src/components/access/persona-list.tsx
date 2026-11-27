"use client";
/**
 * What: the persona table: person and role, "Try this" line with scenario tags, plan and status,
 *   account ID, PIN with copy, and Start. Stacks into labelled rows on narrow screens.
 * Use when: access is granted and GET /api/demo/personas returned the list.
 * Inputs: { personas, onStart, startingIndex, startDisabled }.
 * Output: a list with one row per persona (Figma "Landing: choose a persona": a table, not a card grid).
 * Side effects: the copy button writes the PIN to the clipboard.
 * Role: persona chooser.
 */
import type { Persona } from "@contracts/contracts";
import { Check, ChevronRight, Copy } from "lucide-react";
import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * What: display names for Persona.role.
 * Use when: rendering the role under the name.
 * Inputs: none.
 * Output: a record.
 * Side effects: none.
 * Role: label map.
 */
const ROLE_LABEL: Record<Persona["role"], string> = { owner: "Owner", billing_admin: "Billing admin", member: "Member" };
/**
 * What: display names for Persona.tier.
 * Use when: rendering plan and status.
 * Inputs: none.
 * Output: a record.
 * Side effects: none.
 * Role: label map.
 */
const TIER_LABEL: Record<Persona["tier"], string> = { free: "Free", pro: "Pro", business: "Business", enterprise: "Enterprise" };
/**
 * What: display names for Persona.contract_status.
 * Use when: rendering plan and status.
 * Inputs: none.
 * Output: a record.
 * Side effects: none.
 * Role: label map.
 */
const STATUS_LABEL: Record<Persona["contract_status"], string> = {
  trialing: "trialing",
  active: "active",
  past_due: "past due",
  in_notice: "in notice",
  cancelled: "cancelled",
};

/**
 * What: formats the scenario tags ("S1, S2, S5"; "rules" becomes "Rules").
 * Use when: the "Try this" cell.
 * Inputs: the scenarios list.
 * Output: a string.
 * Side effects: none.
 * Role: formatter.
 */
export function scenarioLabel(scenarios: Persona["scenarios"]): string {
  return scenarios.map((s) => (s === "rules" ? "Rules" : s)).join(", ");
}

/**
 * What: picks the persona to recommend first: the one that runs S1.
 * Use when: marking "Start here" and giving that row the only ember button.
 * Inputs: the persona list.
 * Output: the index, or -1 when no persona has S1.
 * Side effects: none.
 * Role: The One Ember Rule (DESIGN.md s2).
 */
export function recommendedIndex(personas: readonly Persona[]): number {
  return personas.findIndex((p) => p.scenarios.includes("S1"));
}

/**
 * What: the header row; hidden on narrow screens, where each cell carries its own label.
 * Use when: above the rows at md and wider.
 * Inputs: none.
 * Output: JSX.
 * Side effects: none.
 * Role: column captions (aria-hidden; cells have screen-reader labels).
 */
function HeaderRow() {
  return (
    <div
      aria-hidden
      className="hidden border-b border-rule bg-margin px-5 py-2.5 text-label-sm text-muted md:grid md:grid-cols-[196px_minmax(0,1fr)_150px_110px_74px_140px]"
    >
      <span>Person</span>
      <span>Try this</span>
      <span>Plan and status</span>
      <span>Account ID</span>
      <span>PIN</span>
      <span />
    </div>
  );
}

/**
 * What: a caption shown only on narrow screens (and always to screen readers).
 * Use when: labelling a cell value inside a row.
 * Inputs: { children }.
 * Output: JSX span.
 * Side effects: none.
 * Role: responsive cell label.
 */
function CellLabel({ children }: { children: string }) {
  return <span className="text-label-sm text-muted md:sr-only">{children}</span>;
}

/**
 * What: the PIN with a copy button that confirms with "Copied".
 * Use when: the PIN cell of each row.
 * Inputs: { pin, personName }.
 * Output: JSX.
 * Side effects: navigator.clipboard.writeText; a 1.5 s timer resets the label.
 * Role: lets a reviewer paste the PIN into the verification card.
 */
function PinCopy({ pin, personName }: { pin: string; personName: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (state === "idle") return;
    const id = setTimeout(() => setState("idle"), 1500);
    return () => clearTimeout(id);
  }, [state]);

  /**
   * What: copies the PIN.
   * Use when: the copy button is pressed.
   * Inputs: none.
   * Output: none.
   * Side effects: clipboard write; state change.
   * Role: click handler.
   */
  async function copy() {
    try {
      await navigator.clipboard.writeText(pin);
      setState("copied");
    } catch {
      setState("failed");
    }
  }

  return (
    <span className="inline-flex items-center gap-1">
      <span className="font-mono text-mono text-ink tabular">{pin}</span>
      <button
        type="button"
        onClick={copy}
        aria-label={`Copy PIN for ${personName}`}
        className="grid size-6 place-items-center rounded-sm text-muted transition-colors duration-150 hover:bg-margin hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1"
      >
        {state === "copied" ? <Check aria-hidden className="size-3.5 text-success" /> : <Copy aria-hidden className="size-3.5" />}
      </button>
      <span role="status" className="text-label-sm text-muted">
        {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : ""}
      </span>
    </span>
  );
}

/**
 * What: the persona list component.
 * Use when: rendered by Landing after access is granted.
 * Inputs: personas; onStart(index); startingIndex (the row whose Start is pending, or null);
 *   startDisabled (true during a 429 countdown).
 * Output: JSX list.
 * Side effects: clipboard writes from PinCopy.
 * Role: persona chooser.
 */
export function PersonaList({
  personas,
  onStart,
  startingIndex,
  startDisabled,
}: {
  personas: readonly Persona[];
  onStart: (index: number) => void;
  startingIndex: number | null;
  startDisabled: boolean;
}) {
  const baseId = useId();
  const recommended = recommendedIndex(personas);

  return (
    <div className="overflow-hidden rounded-lg border border-rule bg-raised">
      <HeaderRow />
      <ul className="divide-y divide-rule">
        {personas.map((p, i) => {
          const nameId = `${baseId}-${i}`;
          const isRecommended = i === recommended;
          const firstName = p.name.split(" ")[0] ?? p.name;
          const flagged = p.contract_status !== "active" && p.contract_status !== "trialing";
          return (
            <li
              key={`${p.account_id}-${p.name}`}
              aria-labelledby={nameId}
              className="grid grid-cols-2 gap-x-4 gap-y-3 px-5 py-5 md:grid-cols-[196px_minmax(0,1fr)_150px_110px_74px_140px] md:items-center md:gap-x-0 md:gap-y-0 md:py-4"
            >
              <div className="col-span-2 flex flex-col gap-0.5 md:col-span-1">
                <span className="flex flex-wrap items-center gap-2">
                  <span id={nameId} className="text-label text-ink">
                    {p.name}
                  </span>
                  {isRecommended && (
                    <span className="rounded-full bg-ember-soft px-1.5 text-label-sm text-ember">Start here</span>
                  )}
                </span>
                <span className="text-label-sm font-normal text-muted">{ROLE_LABEL[p.role]}</span>
              </div>

              <div className="col-span-2 flex flex-col gap-1 md:col-span-1 md:pr-5">
                <span className="sr-only">Try this, scenarios</span>
                <span className="font-mono text-mono-sm text-muted">{scenarioLabel(p.scenarios)}</span>
                <span className="text-body-sm text-ink">{p.description}</span>
              </div>

              <div className="flex flex-col gap-0.5">
                <CellLabel>Plan and status</CellLabel>
                <span className={cn("text-body-sm", flagged ? "text-warning" : "text-ink")}>
                  {TIER_LABEL[p.tier]} · {STATUS_LABEL[p.contract_status]}
                </span>
              </div>

              <div className="flex flex-col gap-0.5">
                <CellLabel>Account ID</CellLabel>
                <span className="font-mono text-mono text-ink">{p.account_id}</span>
              </div>

              <div className="flex flex-col gap-0.5">
                <CellLabel>PIN</CellLabel>
                <PinCopy pin={p.pin} personName={p.name} />
              </div>

              <div className="flex items-end justify-end md:items-center">
                <Button
                  variant={isRecommended ? "primary" : "quiet"}
                  size="sm"
                  className="h-9 px-3.5"
                  onClick={() => onStart(i)}
                  disabled={startDisabled || startingIndex !== null}
                  aria-label={`Start as ${p.name}`}
                >
                  {startingIndex === i ? "Starting" : isRecommended ? `Start as ${firstName}` : "Start"}
                  <ChevronRight aria-hidden className="size-3.5" />
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
