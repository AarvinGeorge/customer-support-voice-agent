/**
 * What: `/console` route shell: header bar and the empty conversation column.
 * Use when: a visitor opens the console; the conversation, step trail, input bar and
 *   drawer arrive in CS-E-04.S5 and later tasks.
 * Inputs: none.
 * Output: the console frame.
 * Side effects: none.
 * Role: route shell for the chat and voice console.
 */
import type { Metadata } from "next";

import { ThemeToggle } from "@/components/theme-toggle";

/**
 * What: metadata for /console.
 * Use when: Next.js builds the head for this route.
 * Inputs: none.
 * Output: a Metadata object.
 * Side effects: none.
 * Role: route title.
 */
export const metadata: Metadata = { title: "Console · Quillstack Support" };

/**
 * What: the console page component.
 * Use when: route `/console`.
 * Inputs: none.
 * Output: JSX.
 * Side effects: none.
 * Role: route shell.
 */
export default function ConsolePage() {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex h-14 items-center justify-between border-b border-rule px-4">
        <span className="text-heading text-ink">Quillstack Support</span>
        <ThemeToggle />
      </header>
      <main className="mx-auto flex w-full max-w-[44rem] flex-1 flex-col justify-center px-6 py-12">
        <p className="text-body text-muted">The conversation appears here once a session starts.</p>
      </main>
    </div>
  );
}
