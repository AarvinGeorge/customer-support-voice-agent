/**
 * What: `/console` route: the chat console (top bar, conversation log with step trails, input bar).
 * Use when: a visitor opens the console from the landing page (`/console?session=<id>`).
 * Inputs: none (the client entry reads the query).
 * Output: the console frame.
 * Side effects: none on the server.
 * Role: route shell; the work is in components/console (CS-E-04.S5.T1).
 */
import type { Metadata } from "next";
import { Suspense } from "react";

import { ConsoleRoute } from "@/components/console/console-route";

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
 * Role: route shell; Suspense is required around useSearchParams for static rendering.
 */
export default function ConsolePage() {
  return (
    <Suspense>
      <ConsoleRoute />
    </Suspense>
  );
}
