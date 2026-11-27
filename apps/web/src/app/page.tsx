/**
 * What: placeholder home page; the access gate and persona grid replace it in CS-E-04.S1.T1.
 * Use when: a visitor opens `/`.
 * Inputs: none.
 * Output: a short page naming the product with a link to the console.
 * Side effects: none.
 * Role: ACCESS_GATE_PATH target until the real gate lands.
 */
import Link from "next/link";

/**
 * What: the home page component.
 * Use when: route `/`.
 * Inputs: none.
 * Output: JSX.
 * Side effects: none.
 * Role: placeholder screen.
 */
export default function HomePage() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-[34rem] flex-col justify-center gap-6 px-6 py-16">
      <div className="flex flex-col gap-2">
        <h1 className="text-title text-ink">Quillstack Support</h1>
        <p className="text-body text-muted">
          Ask about billing, your account or a problem you are seeing, by chat or by voice. Every step the agent takes is shown.
        </p>
      </div>
      <p className="text-body-sm text-muted">The access code form appears here.</p>
      <Link
        href="/console"
        className="text-label self-start rounded-sm text-ink underline decoration-rule underline-offset-4 transition-colors duration-150 hover:decoration-ink focus-visible:outline-2 focus-visible:outline-offset-2"
      >
        Open the console
      </Link>
    </main>
  );
}
