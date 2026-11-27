"use client";
/**
 * What: the console input bar: a message field that sends on Enter, a send button, and a
 *   disabled mic placeholder (voice arrives with CS-E-05).
 * Use when: the bottom of the console.
 * Inputs: { onSend, busy, disabled, placeholder?, inputRef?, locked?, restore?, caption? }.
 * Output: JSX.
 * Side effects: calls onSend with the trimmed text and clears the field; puts a refused draft
 *   back into an empty field (restore).
 * Role: CS-E-04.S5.T1 input bar (F1 s5.1, s5.4); CS-E-04.S5.T3 adds the 2,000 character counter
 *   (shown from 1,800, blocks sending over the limit; doc 07 s2.3) and the placeholder from the
 *   console (the pending-confirmation prompt).
 */
import { ArrowUp, Mic } from "lucide-react";
import { useEffect, useId, useState, type KeyboardEvent, type Ref } from "react";

import { messageLimit } from "@/lib/turns/furniture";
import { cn } from "@/lib/utils";

/**
 * What: props of InputBar.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `busy` (a turn is in flight: Enter does nothing, text kept), `disabled` (no session) and
 *   `placeholder` (the console's words for the current state; a default when absent).
 */
export interface InputBarProps {
  onSend: (text: string) => void;
  busy: boolean;
  disabled: boolean;
  placeholder?: string;
  /** Ref to the message field, so the shell can return focus to it after a card press. */
  inputRef?: Ref<HTMLTextAreaElement>;
  /** Why sending is paused (a 429 or 503 countdown), or null; typing stays allowed. */
  locked?: string | null;
  /** A draft the agent refused (429 or 503); restored once per id, only into an empty field. */
  restore?: { id: number; text: string } | null;
  /** Replaces the helper line under the bar (the limit caption). */
  caption?: string | null;
}

/**
 * What: the InputBar component.
 * Use when: see module.
 * Inputs: InputBarProps.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module. Enter sends (ignored while an IME is composing); Shift+Enter is a newline.
 */
export function InputBar({ onSend, busy, disabled, placeholder, inputRef, locked = null, restore = null, caption = null }: InputBarProps) {
  const [text, setText] = useState("");
  const limit = messageLimit(text.trim());
  const canSend = !busy && !disabled && locked === null && text.trim() !== "" && !limit.over;
  const restoreId = restore?.id ?? null;

  useEffect(() => {
    if (restore === null) return;
    setText((current) => (current === "" ? restore.text : current));
    // Only a new notice id restores; later renders of the same notice keep what the customer typed.
  }, [restoreId]);
  const counterId = useId();

  /**
   * What: sends the current text if allowed.
   * Use when: Enter or the send button.
   * Inputs: none.
   * Output: none.
   * Side effects: onSend, clears the field.
   * Role: handler.
   */
  const send = () => {
    if (!canSend) return;
    onSend(text.trim());
    setText("");
  };

  /**
   * What: Enter to send, Shift+Enter for a newline.
   * Use when: keydown in the textarea.
   * Inputs: the keyboard event.
   * Output: none.
   * Side effects: may send; prevents the newline on a plain Enter.
   * Role: handler.
   */
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
    e.preventDefault();
    send();
  };

  return (
    <div className="flex w-full flex-col items-center gap-2">
      <div
        className={cn(
          "flex w-full items-center gap-2 rounded-lg border border-rule bg-raised py-2.5 pr-2.5 pl-4 transition-colors duration-150 ease-out-expo focus-within:border-ember",
          limit.over && "border-error focus-within:border-error",
        )}
      >
        <textarea
          ref={inputRef}
          aria-label="Message"
          rows={1}
          value={text}
          disabled={disabled}
          enterKeyHint="send"
          placeholder={placeholder ?? (disabled ? "Start from the access page to open a session" : "Ask about billing, your account or a how-to")}
          aria-describedby={limit.show ? counterId : undefined}
          aria-invalid={limit.over || undefined}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          className="max-h-48 min-h-6 flex-1 resize-none bg-transparent text-body text-ink outline-none placeholder:text-muted max-sm:text-[16px]"
        />
        <button
          type="button"
          disabled
          aria-label="Voice, coming soon"
          title="Voice, coming soon"
          className="flex size-9 shrink-0 items-center justify-center rounded-full border border-rule bg-raised text-ink disabled:opacity-50"
        >
          <Mic aria-hidden className="size-[18px]" />
        </button>
        <button
          type="button"
          aria-label="Send"
          aria-disabled={!canSend}
          title={busy ? "Finishing the current reply" : locked ?? (limit.over ? "Shorten your message to send" : undefined)}
          onClick={send}
          className={cn(
            "flex size-9 shrink-0 items-center justify-center rounded-full bg-ink text-paper outline-none transition-[opacity,transform] duration-150 ease-out-expo focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-raised motion-safe:active:scale-[0.97]",
            !canSend && "opacity-40",
          )}
        >
          <ArrowUp aria-hidden className="size-[18px]" />
        </button>
      </div>
      {limit.show ? (
        <p id={counterId} aria-live={limit.over ? "polite" : undefined} className={cn("text-center text-caption", limit.over ? "text-error" : "text-muted")}>
          <span className="font-mono tabular">{limit.text}</span>
          <span>{limit.over ? " characters. Shorten your message to send." : " characters"}</span>
        </p>
      ) : (
        <p className="text-center text-caption text-muted">{caption ?? "Answers come only from Quillstack help articles. Actions need your clear yes."}</p>
      )}
    </div>
  );
}
