"use client";
/**
 * What: the drawer's Demo controls: two switches that arm a demo fault (`tool_error` or
 *   `llm_429`) for the next turn request only, shown only when GET /api/me says
 *   demo_faults_enabled.
 * Use when: ConsoleShell's `demoControls` slot (the Inspector drawer footer).
 * Inputs: { store, loadMe? } (loadMe defaults to getMe; tests pass a fake).
 * Output: the controls, or nothing while the flag is unknown, false or unreadable.
 * Side effects: one GET /api/me on mount; a switch writes store.armedFault. The store sends
 *   X-Demo-Fault on the next request and clears it (doc 07 s10), so the switch turns itself off.
 * Role: CS-E-07.S1.T4, from the Figma "Demo fault toggle" frame.
 */
import { FlaskConical } from "lucide-react";
import { useEffect, useState } from "react";
import { useStore, type StoreApi } from "zustand";
import type { Me } from "@contracts/contracts";

import { getMe } from "@/lib/api/access";
import type { DemoFault } from "@/lib/api/stream";
import type { TurnStoreState } from "@/lib/turns/store";
import { cn } from "@/lib/utils";

/**
 * What: the two faults with their labels.
 * Use when: rendering the switches.
 * Inputs: none.
 * Output: a frozen list.
 * Side effects: none.
 * Role: copy table (Figma "Demo fault toggle").
 */
const FAULTS: readonly { fault: DemoFault; label: string }[] = Object.freeze([
  { fault: "tool_error", label: "Billing tool error on the next message" },
  { fault: "llm_429", label: "Primary model rate limit (429) on the next message" },
]);

/**
 * What: one switch row: label, the header it sends, and the switch.
 * Use when: DemoControls.
 * Inputs: { label, fault, on, onToggle }.
 * Output: a row with a role="switch" button.
 * Side effects: onToggle on press.
 * Role: control.
 */
function FaultSwitch({ label, fault, on, onToggle }: { label: string; fault: DemoFault; on: boolean; onToggle: () => void }) {
  const id = `fault-${fault}`;
  return (
    <div className="flex items-center gap-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span id={id} className="text-label text-ink">
          {label}
        </span>
        <span className="font-mono text-mono-sm text-muted">X-Demo-Fault: {fault}</span>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-labelledby={id}
        onClick={onToggle}
        className={cn(
          "relative h-5 w-[34px] shrink-0 rounded-full border transition-colors duration-150 ease-out-expo focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-margin focus-visible:outline-none",
          on ? "border-ember bg-ember" : "border-rule bg-rule",
        )}
      >
        <span
          aria-hidden
          className={cn(
            "absolute top-1/2 left-0.5 size-3.5 -translate-y-1/2 rounded-full bg-raised transition-transform duration-150 ease-out-expo",
            on && "translate-x-[14px]",
          )}
        />
      </button>
    </div>
  );
}

/**
 * What: the DemoControls component.
 * Use when: see module.
 * Inputs: { store, loadMe? }.
 * Output: JSX or null.
 * Side effects: see module.
 * Role: see module.
 */
export function DemoControls({ store, loadMe = getMe }: { store: StoreApi<TurnStoreState>; loadMe?: () => Promise<Me> }) {
  const [enabled, setEnabled] = useState(false);
  const armed = useStore(store, (s) => s.armedFault);
  const last = useStore(store, (s) => s.lastFault);

  useEffect(() => {
    let live = true;
    loadMe()
      .then((me) => {
        if (live) setEnabled(me.demo_faults_enabled === true);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [loadMe]);

  if (!enabled) return null;
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-rule bg-margin p-4">
      <p className="flex items-center gap-2 text-label text-ink">
        <FlaskConical aria-hidden className="size-3.5" strokeWidth={1.75} />
        Demo controls · fault injection
      </p>
      {FAULTS.map((f) => (
        <FaultSwitch
          key={f.fault}
          label={f.label}
          fault={f.fault}
          on={armed === f.fault}
          onToggle={() => store.getState().armFault(armed === f.fault ? null : f.fault)}
        />
      ))}
      <p aria-live="polite" className="text-caption text-muted">
        {armed === null && last !== null
          ? `Sent X-Demo-Fault: ${last} with your last message, then switched off.`
          : "Applies to your next message only, then switches off. Faults can make a call fail; they can never make a change happen."}
      </p>
    </div>
  );
}
