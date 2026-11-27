"use client";
/**
 * What: the console input bar: a message field that sends on Enter, a send button, and a
 *   disabled mic placeholder (voice arrives with CS-E-05).
 * Use when: the bottom of the console.
 * Inputs: { onSend, busy, disabled }.
 * Output: JSX.
 * Side effects: calls onSend with the trimmed text and clears the field.
 * Role: CS-E-04.S5.T1 input bar (F1 s5.1, s5.4); the 2,000 limit counter and pending placeholder
 *   are CS-E-04.S5.T3.
 */
import { ArrowUp, Mic } from "lucide-react";
import { useState, type KeyboardEvent, type Ref } from "react";

import { cn } from "@/lib/utils";

/**
 * What: props of InputBar.
 * Use when: typing the component.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: `busy` (a turn is in flight: Enter does nothing, text kept) and `disabled` (no session).
 */
export interface InputBarProps {
  onSend: (text: string) => void;
  busy: boolean;
  disabled: boolean;
  /** Ref to the message field, so the shell can return focus to it after a card press. */
  inputRef?: Ref<HTMLTextAreaElement>;
}

/**
 * What: the InputBar component.
 * Use when: see module.
 * Inputs: InputBarProps.
 * Output: JSX.
 * Side effects: see module.
 * Role: see module. Enter sends (ignored while an IME is composing); Shift+Enter is a newline.
 */
export function InputBar({ onSend, busy, disabled, inputRef }: InputBarProps) {
  const [text, setText] = useState("");
  const canSend = !busy && !disabled && text.trim() !== "";

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
      <div className="flex w-full items-center gap-2 rounded-lg border border-rule bg-raised py-2.5 pr-2.5 pl-4 transition-colors duration-150 ease-out-expo focus-within:border-ember">
        <textarea
          ref={inputRef}
          aria-label="Message"
          rows={1}
          value={text}
          disabled={disabled}
          enterKeyHint="send"
          placeholder={disabled ? "Start from the access page to open a session" : "Ask about billing, your account or a how-to"}
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
          title={busy ? "Finishing the current reply" : undefined}
          onClick={send}
          className={cn(
            "flex size-9 shrink-0 items-center justify-center rounded-full bg-ink text-paper outline-none transition-[opacity,transform] duration-150 ease-out-expo focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-raised motion-safe:active:scale-[0.97]",
            !canSend && "opacity-40",
          )}
        >
          <ArrowUp aria-hidden className="size-[18px]" />
        </button>
      </div>
      <p className="text-center text-caption text-muted">Answers come only from Quillstack help articles. Actions need your clear yes.</p>
    </div>
  );
}
