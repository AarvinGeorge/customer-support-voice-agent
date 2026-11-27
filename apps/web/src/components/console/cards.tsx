"use client";
/**
 * What: the message cards and lines of CS-E-04.S6: the masked verification card, the
 *   confirmation card (read-back, amount, invoice, charge date, rule, expiry, Cancel and
 *   Confirm), the verification result line and the receipt line per action outcome.
 * Use when: the conversation log renders an agent turn (chat or voice; the cards show in both).
 * Inputs: reducer events, card states from lib/turns/cards.ts, and send callbacks.
 * Output: JSX.
 * Side effects: a press sends one TurnRequest through the callback; the confirmation card ticks
 *   a 1 s timer while open.
 * Role: CS-E-04.S6.T1, laid out from the Figma page "Console, chat (S1)" and DESIGN.md "Cards":
 *   Raised surface, Rule border, 14px radius, never nested; the ember Confirm is the only ember.
 *   The PIN lives only in this card's field state until the press, which clears it before the
 *   request is sent; it never reaches the store, the thread or the DOM after submit.
 */
import {
  CircleAlert,
  CircleCheck,
  CircleSlash,
  Clock,
  Lock,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent, type RefObject } from "react";
import type {
  ConfirmInput,
  ConfirmationRequiredEvent,
  VerificationInput,
  VerificationRequiredEvent,
  VerificationResultEvent,
} from "@contracts/contracts";

import { Button } from "@/components/ui/button";
import {
  confirmBody,
  confirmationCardState,
  formatAmount,
  formatChargeDate,
  formatCountdown,
  receiptFor,
  verificationBody,
  verificationNotice,
  type CardIndex,
  type CardTone,
  type Receipt,
} from "@/lib/turns/cards";
import { cn } from "@/lib/utils";

/**
 * What: the card surface classes (DESIGN.md "Cards").
 * Use when: both cards.
 * Inputs: none.
 * Output: a class string.
 * Side effects: none.
 * Role: style constant.
 */
const CARD = "flex w-full flex-col rounded-lg border border-rule bg-raised p-5";

/**
 * What: text color per tone.
 * Use when: result and receipt icons.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: style table (words always carry the meaning).
 */
const TONE_TEXT: Readonly<Record<CardTone, string>> = Object.freeze({
  success: "text-success",
  warning: "text-warning",
  error: "text-error",
  muted: "text-muted",
});

/**
 * What: icon per receipt tone.
 * Use when: receipt lines and closed card footers.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: icon table (F1 s4.4: check, clock, alert, slash).
 */
const RECEIPT_ICONS: Readonly<Record<CardTone, LucideIcon>> = Object.freeze({
  success: CircleCheck,
  warning: Clock,
  error: CircleAlert,
  muted: CircleSlash,
});

/**
 * What: icon per verification result.
 * Use when: the verification result line.
 * Inputs: none.
 * Output: a frozen map.
 * Side effects: none.
 * Role: icon table.
 */
const VERIFY_ICONS: Readonly<Record<VerificationResultEvent["status"], LucideIcon>> = Object.freeze({
  verified: ShieldCheck,
  failed: ShieldX,
  locked: Lock,
  conflict: ShieldAlert,
  unavailable: ShieldAlert,
});

/**
 * What: one line under a turn for a verification.result (Figma "Verification result").
 * Use when: a turn carries verification.result.
 * Inputs: { result }.
 * Output: a paragraph (no live role: the console's one status region announces, review PR #20 M3).
 * Side effects: none.
 * Role: result line.
 */
export function VerificationResultLine({ result }: { result: VerificationResultEvent }) {
  const n = verificationNotice(result);
  const Icon = VERIFY_ICONS[n.status];
  return (
    <p data-slot="verification-result" className="inline-flex items-start gap-2 text-label text-ink">
      <Icon aria-hidden className={cn("mt-px size-4 shrink-0", TONE_TEXT[n.tone])} />
      <span>{n.text}</span>
    </p>
  );
}

/**
 * What: one receipt line for an action outcome (Figma "Receipt line").
 * Use when: a turn carries action.result, and as the closed card footer.
 * Inputs: { receipt, standalone } (standalone marks the line under its own turn with
 *   data-slot="receipt"; the closed card footer passes false).
 * Output: a paragraph (no live role: the console's one status region announces, review PR #20 M3).
 * Side effects: none.
 * Role: receipt line.
 */
export function ReceiptLine({ receipt, standalone = true }: { receipt: Receipt; standalone?: boolean }) {
  const Icon = RECEIPT_ICONS[receipt.tone];
  return (
    <p data-slot={standalone ? "receipt" : undefined} className="inline-flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <Icon aria-hidden className={cn("size-4 shrink-0", TONE_TEXT[receipt.tone])} />
      <span className="text-label text-ink">{receipt.title}</span>
      {receipt.detail && <span className="font-mono text-mono-sm text-muted">{receipt.detail}</span>}
    </p>
  );
}

/**
 * What: runs one async press at a time and ignores presses while one is in flight.
 * Use when: Verify, Confirm and Cancel (a double click must send one request).
 * Inputs: none.
 * Output: { busy, run } where run(fn) calls fn only if nothing is in flight.
 * Side effects: none beyond calling fn; the guard is a ref, so two clicks in the same frame
 *   cannot both pass.
 * Role: hook. The guard lifts when the request settles, so a dropped request can be retried.
 */
function useSinglePress() {
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const run = (fn: () => Promise<unknown>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    fn()
      .catch(() => undefined)
      .finally(() => {
        inFlight.current = false;
        setBusy(false);
      });
  };
  return { busy, run };
}

/**
 * What: props of VerificationCard.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `request` is the open verification.required (null in the locked state); `state` from
 *   verificationCardState; `disabled` while any turn streams; `account` and `onAccountChange`
 *   keep the Account ID outside the card so a retry card shows it again (never the PIN);
 *   `pinRef` lets the shell focus the PIN of a retry card; `onSubmit` sends
 *   TurnRequest.verification and resolves when the turn settles.
 */
export interface VerificationCardProps {
  request: VerificationRequiredEvent | null;
  state: "form" | "locked";
  disabled: boolean;
  account: string;
  onAccountChange: (value: string) => void;
  pinRef: RefObject<HTMLInputElement | null>;
  onSubmit: (body: VerificationInput) => Promise<unknown>;
}

/**
 * What: the masked verification card (Figma "Verification card").
 * Use when: a turn hosts the open verification request (doc 07 s6.3).
 * Inputs: VerificationCardProps.
 * Output: a labelled region with the form, or the locked state.
 * Side effects: on Verify, empties the PIN field, then calls onSubmit once.
 * Role: see module. Account ID is a plain text field (as in Figma). The PIN is an uncontrolled
 *   password field (so React never writes it to the value attribute, review PR #24 M1), digits
 *   only, 4 at most, autocomplete "one-time-code" so password managers do not offer to save it
 *   (M2); only its length is kept in state.
 */
export function VerificationCard({ request, state, disabled, account, onAccountChange, pinRef, onSubmit }: VerificationCardProps) {
  const headingId = useId();
  const accountId = useId();
  const pinId = useId();
  const helpId = useId();
  const attemptsId = useId();
  const [pinLength, setPinLength] = useState(0);
  const press = useSinglePress();
  const canVerify = verificationBody(account, "0".repeat(pinLength)) !== null && !disabled && !press.busy;

  /**
   * What: keeps the PIN field to at most 4 digits, in place.
   * Use when: the PIN field changes.
   * Inputs: the change event.
   * Output: none.
   * Side effects: rewrites the field's live value when it held other characters; stores the length.
   * Role: handler.
   */
  const onPinChange = (e: ChangeEvent<HTMLInputElement>) => {
    const el = e.currentTarget;
    const clean = el.value.replace(/\D/g, "").slice(0, 4);
    if (clean !== el.value) el.value = clean;
    setPinLength(clean.length);
  };

  /**
   * What: sends the verification body.
   * Use when: the form submits (Verify or Enter).
   * Inputs: the form event.
   * Output: none.
   * Side effects: empties the PIN field before the request; one onSubmit call per press.
   * Role: handler.
   */
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const field = pinRef.current;
    const b = verificationBody(account, field?.value ?? "");
    if (b === null || disabled || field === null) return;
    field.value = "";
    setPinLength(0);
    press.run(() => onSubmit(b));
  };

  const left = request?.attempts_left ?? null;
  const described = `${helpId} ${attemptsId}`;
  return (
    <section aria-labelledby={headingId} className={cn(CARD, "max-w-[28.75rem] gap-3.5")}>
      <h3 id={headingId} className="inline-flex items-center gap-2 text-heading text-ink">
        <Lock aria-hidden className="size-4 shrink-0" />
        Verify your account
      </h3>
      {state === "locked" ? (
        <p className="text-body-sm text-muted">Verification is locked for this conversation.</p>
      ) : (
        <>
          <p id={helpId} className="max-w-[26.25rem] text-body-sm text-muted">
            Your PIN is checked by our account system. It never appears in the chat or the trace.
          </p>
          <form onSubmit={submit} className="flex flex-col gap-3.5" autoComplete="off">
            <div className="flex flex-wrap gap-3">
              <div className="flex min-w-0 flex-1 flex-col gap-1.5 sm:max-w-[13.75rem]">
                <label htmlFor={accountId} className="text-label-sm text-muted">
                  Account ID
                </label>
                <input
                  id={accountId}
                  type="text"
                  value={account}
                  onChange={(e) => onAccountChange(e.target.value)}
                  required
                  aria-describedby={described}
                  autoComplete="off"
                  autoCapitalize="characters"
                  spellCheck={false}
                  maxLength={20}
                  placeholder="QS-00000"
                  className="h-10 w-full rounded-[8px] border border-rule bg-paper px-3 font-mono text-mono text-ink uppercase outline-none transition-colors duration-150 ease-out-expo placeholder:text-muted/70 focus-visible:border-ember max-sm:text-[16px]"
                />
              </div>
              <div className="flex w-[7.5rem] flex-col gap-1.5">
                <label htmlFor={pinId} className="text-label-sm text-muted">
                  Support PIN
                </label>
                <input
                  id={pinId}
                  ref={pinRef}
                  type="password"
                  inputMode="numeric"
                  onChange={onPinChange}
                  required
                  aria-describedby={described}
                  autoComplete="one-time-code"
                  maxLength={4}
                  placeholder="••••"
                  className="h-10 w-full rounded-[8px] border border-rule bg-paper px-3 font-mono text-mono tracking-[0.3em] text-ink outline-none transition-colors duration-150 ease-out-expo placeholder:text-muted/70 focus-visible:border-ember max-sm:text-[16px]"
                />
              </div>
            </div>
            <div className="flex items-center gap-3">
              <Button type="submit" variant="ink" size="sm" disabled={!canVerify}>
                {press.busy ? "Verifying..." : "Verify"}
              </Button>
              <span id={attemptsId} className="text-caption text-muted">
                {left !== null ? `${left} ${left === 1 ? "attempt" : "attempts"} left` : ""}
              </span>
            </div>
          </form>
        </>
      )}
    </section>
  );
}

/**
 * What: props of ConfirmationCard.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `index` and `ruleTitle` come from lib/turns/cards.ts; `receivedAt` is the client time
 *   the store applied the event (TurnView.confirmationReceivedAt); `disabled` while any turn streams;
 *   `onDecide` sends TurnRequest.confirm and resolves when the turn settles.
 */
export interface ConfirmationCardProps {
  confirmation: ConfirmationRequiredEvent;
  turnId: string;
  index: CardIndex;
  ruleTitle: string | null;
  receivedAt: number | null;
  disabled: boolean;
  onDecide: (body: ConfirmInput) => Promise<unknown>;
}

/**
 * What: the client-clock deadline of a pending action.
 * Use when: rendering the card.
 * Inputs: the confirmation event, the client time the event was applied (or null).
 * Output: epoch ms, or null when there is no expires_at or a time does not parse.
 * Side effects: none.
 * Role: helper. The server's own gap (expires_at minus the event ts) added to the client receipt
 *   time, so a skewed client clock does not shorten or stretch the countdown and a remount does
 *   not restart it (review PR #24 I1). With no receipt time (a replay), the event ts is the
 *   anchor, which makes the deadline expires_at itself.
 */
export function deadlineOf(ev: ConfirmationRequiredEvent, receivedAt: number | null): number | null {
  if (!ev.pending.expires_at) return null;
  const sent = Date.parse(ev.ts);
  const gap = Date.parse(ev.pending.expires_at) - sent;
  const anchor = receivedAt ?? sent;
  return Number.isFinite(gap) && Number.isFinite(anchor) ? anchor + gap : null;
}

/**
 * What: one label and value pair in the card's fact row.
 * Use when: Amount, Invoice, Charge date, Rule.
 * Inputs: { label, value, note }.
 * Output: a <div> pair.
 * Side effects: none.
 * Role: layout helper.
 */
function Fact({ label, value, note }: { label: string; value: string; note?: string | null }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-label-sm text-muted">{label}</dt>
      <dd className="font-mono text-mono font-medium text-ink tabular">{value}</dd>
      {note && <dd className="text-caption text-muted">{note}</dd>}
    </div>
  );
}

/**
 * What: the confirmation card (Figma "Confirmation card").
 * Use when: a turn carries confirmation.required.
 * Inputs: ConfirmationCardProps.
 * Output: a labelled region: title, expiry, read-back, facts, then Cancel and Confirm while open,
 *   or a closed footer in words (the outcome, "Replaced by a newer request." or expired).
 * Side effects: a 1 s timer while open; a press calls onDecide once.
 * Role: see module. Confirm is the ember primary and carries `confirm_label`.
 */
export function ConfirmationCard({ confirmation, turnId, index, ruleTitle, receivedAt, disabled, onDecide }: ConfirmationCardProps) {
  const headingId = useId();
  const pending = confirmation.pending;
  const deadline = deadlineOf(confirmation, receivedAt);
  const [now, setNow] = useState(() => Date.now());
  const state = confirmationCardState(pending, turnId, index, now, deadline);
  const open = state.kind === "open";
  const press = useSinglePress();

  useEffect(() => {
    if (!open || deadline === null) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [open, deadline]);

  /**
   * What: sends one decision.
   * Use when: Confirm or Cancel.
   * Inputs: the decision.
   * Output: none.
   * Side effects: one onDecide call per press (single-press guard).
   * Role: handler.
   */
  const decide = (decision: ConfirmInput["decision"]) => press.run(() => onDecide(confirmBody(pending.pending_action_id, decision)));

  const locked = disabled || press.busy;
  return (
    <section aria-labelledby={headingId} data-state={state.kind} className={cn(CARD, "max-w-[35rem] gap-4")}>
      <div className="flex items-start gap-2">
        <Wallet aria-hidden className={cn("mt-1 size-4 shrink-0", open ? "text-ink" : "text-muted")} />
        <h3 id={headingId} className={cn("min-w-0 flex-1 text-heading", open ? "text-ink" : "text-muted")}>
          {pending.title}
        </h3>
        {open && deadline !== null && (
          <span className="mt-1 shrink-0 font-mono text-mono-sm text-muted tabular">Expires in {formatCountdown(deadline - now)}</span>
        )}
      </div>
      <p className={cn("max-w-[65ch] text-body", open ? "text-ink" : "text-muted")}>&ldquo;{pending.readback_text}&rdquo;</p>
      <dl className="flex flex-wrap gap-x-8 gap-y-3">
        {typeof pending.amount_cents === "number" && <Fact label="Amount" value={formatAmount(pending.amount_cents)} />}
        {pending.invoice_ref && <Fact label="Invoice" value={pending.invoice_ref} />}
        {pending.charge_date && <Fact label="Charge date" value={formatChargeDate(pending.charge_date)} />}
        <Fact label="Rule" value={pending.reason_code} note={ruleTitle} />
      </dl>
      {open ? (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button variant="quiet" size="sm" disabled={locked} onClick={() => decide("cancel")}>
            Cancel
          </Button>
          <Button variant="primary" size="sm" disabled={locked} onClick={() => decide("confirm")}>
            {confirmation.confirm_label}
          </Button>
        </div>
      ) : (
        <div className="border-t border-rule pt-3">
          {state.kind === "resolved" && <ReceiptLine receipt={receiptFor(state.result, pending)} standalone={false} />}
          {state.kind === "superseded" && <p className="text-body-sm text-muted">Replaced by a newer request.</p>}
          {state.kind === "expired" && <p className="text-body-sm text-muted">This request expired. Ask again if you still need it.</p>}
        </div>
      )}
    </section>
  );
}
