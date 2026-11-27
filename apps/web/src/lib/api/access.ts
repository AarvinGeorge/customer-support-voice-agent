/**
 * What: typed calls for the landing page: access code, persona list, new session (doc 07 s2.2).
 * Use when: the access gate and persona list talk to the agent.
 * Inputs: the code or nothing, plus optional ApiDeps for tests.
 * Output: promises of the contract bodies (Persona list, SessionCreated) or void.
 * Side effects: network requests through apiFetch; 401 is always thrown to the caller
 *   (on401 "throw"), because the landing page itself is the access gate.
 * Role: the only place the web app names these three routes.
 */
import type { AccessRequest, Persona, SessionCreated } from "@contracts/contracts";

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
 * Use when: the visitor presses Start on a persona row.
 * Inputs: deps.
 * Output: SessionCreated {session_id, created_at}.
 * Side effects: network request; the agent creates a session bound to this visitor.
 * Role: session start; throws ApiError 401 or 429 IP_SESSIONS.
 */
export async function createSession(deps: ApiDeps = {}): Promise<SessionCreated> {
  return apiFetch<SessionCreated>("/api/sessions", { method: "POST", body: {} }, { ...deps, on401: "throw" });
}

/**
 * What: the console URL for a session.
 * Use when: navigating after createSession.
 * Inputs: the session id.
 * Output: "/console?session=<id>" (id URL-encoded).
 * Side effects: none.
 * Role: one place that fixes the console route shape.
 */
export function consolePath(sessionId: string): string {
  return `/console?session=${encodeURIComponent(sessionId)}`;
}
