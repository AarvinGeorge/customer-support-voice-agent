/**
 * What: typed calls for the landing page and console: access code, persona list, new session,
 *   and the visitor's flags from GET /api/me (doc 07 s2.2).
 * Use when: the access gate, persona list and the drawer's Demo controls talk to the agent.
 * Inputs: the code or nothing, plus optional ApiDeps for tests.
 * Output: promises of the contract bodies (Persona list, SessionCreated, Me) or void.
 * Side effects: network requests through apiFetch; 401 is always thrown to the caller
 *   (on401 "throw"), because the landing page itself is the access gate.
 * Role: the only place the web app names these four routes.
 */
import type { AccessRequest, Me, Persona, SessionCreated } from "@contracts/contracts";

import { apiFetch, type ApiDeps } from "./client";

/**
 * What: POST /api/access with the visitor's code.
 * Use when: the visitor presses Continue on the gate.
 * Inputs: code (1..64 characters), deps.
 * Output: resolves on 204 (the agent set the qs_access cookie).
 * Side effects: network request; the browser stores the HttpOnly cookie.
 * Role: access gate call; throws ApiError 401 ACCESS_DENIED or 429 ACCESS_ATTEMPTS.
 */
export async function submitAccessCode(code: string, deps: ApiDeps = {}): Promise<void> {
  const body: AccessRequest = { code };
  await apiFetch<void>("/api/access", { method: "POST", body }, { ...deps, on401: "throw" });
}

/**
 * What: GET /api/demo/personas.
 * Use when: the page loads (to learn whether the cookie is valid) and after a good code.
 * Inputs: deps.
 * Output: the persona list, PINs included (shown on purpose, demo data).
 * Side effects: network request.
 * Role: the only source of persona PINs in the browser; never bundle them.
 */
export async function listPersonas(deps: ApiDeps = {}): Promise<Persona[]> {
  return apiFetch<Persona[]>("/api/demo/personas", {}, { ...deps, on401: "throw" });
}

/**
 * What: POST /api/sessions with an empty body.
 * Use when: the console opens without a session, or New conversation is pressed.
 * Inputs: deps.
 * Output: SessionCreated {session_id, created_at}.
 * Side effects: network request; the agent creates a session bound to this visitor.
 * Role: session start; throws ApiError 401 or 429 IP_SESSIONS.
 */
export async function createSession(deps: ApiDeps = {}): Promise<SessionCreated> {
  return apiFetch<SessionCreated>("/api/sessions", { method: "POST", body: {} }, { ...deps, on401: "throw" });
}

/**
 * What: GET /api/me: the visitor's flags.
 * Use when: the drawer's Demo controls decide whether to show the fault toggle.
 * Inputs: deps.
 * Output: Me {visitor, operator, demo_faults_enabled}.
 * Side effects: network request.
 * Role: flag read (doc 07 s10: the toggle shows only when demo_faults_enabled is true); throws
 *   ApiError 401 instead of redirecting, so a failed read only hides the toggle.
 */
export async function getMe(deps: ApiDeps = {}): Promise<Me> {
  return apiFetch<Me>("/api/me", {}, { ...deps, on401: "throw" });
}

/**
 * What: the display-only start hint carried in a console URL: the account id (`as`) and the person
 *   (`who`, the persona's display name, unique per persona in the seeds).
 * Use when: building a console URL from a landing row (PersonaList) or carrying it to a new session.
 * Inputs: none (type).
 * Output: none (type).
 * Side effects: none.
 * Role: F13 mechanism: an account id alone names a company, and people share accounts, so the hint
 *   also carries the person. `who` is null for an account-only link. Never sent to the server.
 */
export interface PathHint {
  as: string;
  who: string | null;
}

/**
 * What: appends the hint's query parameters (`as`, and `who` when present).
 * Use when: consolePath and consoleStartPath.
 * Inputs: the hint.
 * Output: "as=<id>" or "as=<id>&who=<name>", URL-encoded.
 * Side effects: none.
 * Role: one place that fixes the hint query shape.
 */
function hintQuery(hint: PathHint): string {
  const base = `as=${encodeURIComponent(hint.as)}`;
  return hint.who === null || hint.who === "" ? base : `${base}&who=${encodeURIComponent(hint.who)}`;
}

/**
 * What: the console URL for a session, with the optional display-only hint (D-076).
 * Use when: navigating after createSession (the console's own start, New conversation).
 * Inputs: the session id; the hint (account id and person) or null.
 * Output: "/console?session=<id>", plus "&as=<account id>[&who=<name>]" when a hint is given.
 * Side effects: none.
 * Role: one place that fixes the console route shape. The hint only pre-fills the verification
 *   card; it is never sent to the server.
 */
export function consolePath(sessionId: string, hint: PathHint | null = null): string {
  const base = `/console?session=${encodeURIComponent(sessionId)}`;
  return hint === null || hint.as === "" ? base : `${base}&${hintQuery(hint)}`;
}

/**
 * What: the URL a landing Start link opens in a new tab: the console without a session (D-076).
 * Use when: the landing page's "Start a conversation" (no hint) and each persona row's Start
 *   (that persona's account id and name as the display-only hint).
 * Inputs: the hint or null.
 * Output: "/console" or "/console?as=<account id>&who=<name>".
 * Side effects: none.
 * Role: the console creates the fresh session itself, so every click is a new conversation.
 */
export function consoleStartPath(hint: PathHint | null = null): string {
  return hint === null || hint.as === "" ? "/console" : `/console?${hintQuery(hint)}`;
}

/**
 * What: resolves a start hint against the persona list into what the card may show.
 * Use when: ConsoleRoute has the persona list and the `as` and `who` URL parameters.
 * Inputs: the personas, `as`, `who` (or null).
 * Output: { name, accountId } with the person's name when `who` names a persona on that account;
 *   name null when the account is known but the person is not named (account-only link or a name
 *   that does not belong to it); null when no persona has that account.
 * Side effects: none.
 * Role: F13: the person is matched by (account, name), never by account alone, and an account
 *   shared by several people is never guessed at.
 */
export function resolveStartHint(
  personas: readonly { name: string; account_id: string }[],
  as: string,
  who: string | null,
): { name: string | null; accountId: string } | null {
  const onAccount = personas.filter((p) => p.account_id === as);
  if (onAccount.length === 0) return null;
  const person = who === null ? undefined : onAccount.find((p) => p.name === who);
  if (person !== undefined) return { name: person.name, accountId: person.account_id };
  // An account-only link names the person only when exactly one persona is on the account.
  return { name: who === null && onAccount.length === 1 ? onAccount[0]!.name : null, accountId: as };
}
