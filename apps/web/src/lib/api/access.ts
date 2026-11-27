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
 * What: the console URL for a session, with the optional display-only hint (D-076).
 * Use when: navigating after createSession (the console's own start, New conversation).
 * Inputs: the session id; the hint (a persona's account id, or null).
 * Output: "/console?session=<id>", plus "&as=<account id>" when a hint is given (both URL-encoded).
 * Side effects: none.
 * Role: one place that fixes the console route shape. The hint only pre-fills the verification
 *   card; it is never sent to the server.
 */
export function consolePath(sessionId: string, hint: string | null = null): string {
  const base = `/console?session=${encodeURIComponent(sessionId)}`;
  return hint === null || hint === "" ? base : `${base}&as=${encodeURIComponent(hint)}`;
}

/**
 * What: the URL a landing Start link opens in a new tab: the console without a session (D-076).
 * Use when: the landing page's "Start a conversation" (no hint) and each persona row's Start
 *   (that persona's account id as the display-only hint).
 * Inputs: the hint (account id) or null.
 * Output: "/console" or "/console?as=<account id>".
 * Side effects: none.
 * Role: the console creates the fresh session itself, so every click is a new conversation.
 */
export function consoleStartPath(hint: string | null = null): string {
  return hint === null || hint === "" ? "/console" : `/console?as=${encodeURIComponent(hint)}`;
}
