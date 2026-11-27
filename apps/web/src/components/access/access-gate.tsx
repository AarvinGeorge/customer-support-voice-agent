"use client";
/**
 * What: the access gate: one code field, Continue, the error or 429 countdown, the recording notice.
 * Use when: the visitor has no valid qs_access cookie (doc 07 s2.1).
 * Inputs: { onGranted } called after a 204 from POST /api/access.
 * Output: the gate screen (Figma "Landing: access gate").
 * Side effects: POST /api/access; a countdown interval after a 429.
 * Role: first screen of the demo.
 */
import { ChevronRight, Lock } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { submitAccessCode } from "@/lib/api/access";
import { ApiError } from "@/lib/api/client";

import { BrandMark, RecordingNotice, useCountdown } from "./parts";

/**
 * What: the most characters an access code may have (doc 07 s2.2 AccessRequest code 1..64).
 * Use when: the input maxLength.
 * Inputs: none.
 * Output: 64.
 * Side effects: none.
 * Role: constant.
 */
const CODE_MAX = 64;

/**
 * What: message shown when the response is not a contract error (network down, proxy page).
 * Use when: submitAccessCode throws something other than ApiError 401 or 429.
 * Inputs: none.
 * Output: a sentence.
 * Side effects: none.
 * Role: honest fallback state.
 */
const UNREACHABLE = "The demo service did not answer. Wait a moment and try again.";

/**
 * What: the gate component.
 * Use when: rendered by Landing while access is not granted.
 * Inputs: { onGranted }.
 * Output: JSX.
 * Side effects: see module.
 * Role: access gate screen.
 */
export function AccessGate({ onGranted }: { onGranted: () => void }) {
  const fieldId = useId();
  const messageId = useId();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [limited, setLimited] = useState<string | null>(null);
  const countdown = useCountdown();
  const waiting = limited !== null && countdown.remaining > 0;

  /**
   * What: submits the code and maps the result to a state.
   * Use when: the form submits.
   * Inputs: the submit event.
   * Output: none.
   * Side effects: network request; sets error, countdown or calls onGranted.
   * Role: form handler.
   */
  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = code.trim();
    if (trimmed === "" || busy || waiting) return;
    setBusy(true);
    setError(null);
    setLimited(null);
    try {
      await submitAccessCode(trimmed);
      onGranted();
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setLimited(err.body.message);
        countdown.start(err.retryAfterS ?? 60);
      } else if (err instanceof ApiError && err.status === 401) {
        setError(err.body.message);
      } else {
        setError(UNREACHABLE);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-[25rem] flex-col justify-center gap-7 px-4 py-16">
      <BrandMark />
      <div className="flex flex-col gap-2">
        <h1 className="text-[24px] leading-8 font-semibold tracking-[-0.01em] text-ink">Support agent demo</h1>
        <p className="text-body text-muted">
          A Quillstack support agent you can talk to by chat or voice. It shows every step it takes. Enter the access code
          from your invitation to continue.
        </p>
      </div>
      <form onSubmit={onSubmit} noValidate className="flex flex-col gap-7">
        <div className="flex flex-col gap-1.5">
          <label htmlFor={fieldId} className="text-label text-ink">
            Access code
          </label>
          <div className="relative">
            <Lock aria-hidden className="pointer-events-none absolute top-1/2 left-4 size-3.5 -translate-y-1/2 text-muted" />
            <input
              id={fieldId}
              name="code"
              type="text"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              autoFocus
              maxLength={CODE_MAX}
              value={code}
              onChange={(e) => {
                setCode(e.target.value);
                if (error) setError(null);
              }}
              aria-invalid={error !== null}
              aria-describedby={error || waiting ? messageId : undefined}
              placeholder="Code from your invitation"
              className="h-11 w-full rounded-md border border-rule bg-raised pr-4 pl-10 font-mono text-mono text-ink transition-colors duration-150 outline-none placeholder:font-sans placeholder:text-body-sm placeholder:text-muted focus-visible:border-ember focus-visible:ring-2 focus-visible:ring-ember/25 aria-invalid:border-error"
            />
          </div>
          {error && (
            <p id={messageId} role="alert" className="text-body-sm text-error">
              {error}
            </p>
          )}
          {waiting && (
            <p id={messageId} role="status" className="text-body-sm text-info">
              {limited} Try again in {countdown.remaining} s.
            </p>
          )}
        </div>
        <Button type="submit" variant="ink" className="h-10 w-full" disabled={busy || waiting || code.trim() === ""}>
          {busy ? "Checking" : "Continue"}
          <ChevronRight aria-hidden className="size-3.5" />
        </Button>
      </form>
      <RecordingNotice />
    </main>
  );
}
