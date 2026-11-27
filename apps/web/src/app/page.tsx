/**
 * What: home page `/`: the access gate and the persona list (CS-E-04.S1.T1).
 * Use when: a visitor opens the demo URL, or a 401 sends them back here (ACCESS_GATE_PATH).
 * Inputs: none.
 * Output: the Landing client component.
 * Side effects: none on the server; Landing calls the API in the browser.
 * Role: route entry. It holds no persona data: PINs arrive only from GET /api/demo/personas.
 */
import { Landing } from "@/components/access/landing";

/**
 * What: the home page component.
 * Use when: route `/`.
 * Inputs: none.
 * Output: JSX.
 * Side effects: none.
 * Role: route entry.
 */
export default function HomePage() {
  return <Landing />;
}
