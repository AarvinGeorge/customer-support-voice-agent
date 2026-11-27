/**
 * What: DEV-ONLY mock of the agent's landing routes (doc 07 s2.2): POST /api/access,
 *   GET /api/demo/personas, POST /api/sessions, GET /api/me.
 * Use when: `pnpm dev:mock` (next dev with QS_MOCK_API=1), so the landing page renders without
 *   the agent service. It is NOT a route in any other mode: the `.dev.ts` extension is only in
 *   pageExtensions for the dev server with QS_MOCK_API=1 (next.config.ts), so `next build`
 *   never compiles it and the PIN fixtures stay out of the production output.
 * Inputs: the request; a non-HttpOnly `qs_mock_access` cookie stands in for `qs_access`.
 * Output: contract-shaped JSON bodies and ErrorEnvelopes.
 * Side effects: sets the mock cookie on a good code.
 * Role: local stand-in for lane B's CS-E-04.S1.T3 endpoints. Codes: MOCK_ACCESS_CODE grants
 *   access, MOCK_RATE_LIMITED_CODE answers 429 ACCESS_ATTEMPTS, anything else 401.
 */
import { MOCK_ACCESS_CODE, MOCK_PERSONAS, MOCK_RATE_LIMITED_CODE } from "@/dev-mock/fixtures";

/**
 * What: the cookie name the mock uses in place of qs_access.
 * Use when: granting and checking mock access.
 * Inputs: none.
 * Output: a string.
 * Side effects: none.
 * Role: constant.
 */
const COOKIE = "qs_mock_access";

/**
 * What: a doc 07 s1.3 error Response.
 * Use when: the mock answers a non-2xx.
 * Inputs: status, class, code, message, retry_after_s.
 * Output: a Response with the envelope (and Retry-After on 429).
 * Side effects: none.
 * Role: helper.
 */
function fail(status: number, cls: string, code: string, message: string, retryAfter?: number): Response {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (retryAfter !== undefined) headers["retry-after"] = String(retryAfter);
  const error = {
    class: cls,
    code,
    message,
    retryable: status === 429,
    changed: false,
    retry_after_s: retryAfter ?? null,
    fault_injected: false,
    trace_id: null,
    details: status === 429 ? { limit: "10/hour", scope: "ip" } : {},
  };
  return new Response(JSON.stringify({ error }), { status, headers });
}

/**
 * What: whether the request carries the mock access cookie.
 * Use when: every route except /api/access.
 * Inputs: the request.
 * Output: boolean.
 * Side effects: none.
 * Role: helper.
 */
function hasAccess(req: Request): boolean {
  return (req.headers.get("cookie") ?? "").split(/;\s*/).includes(`${COOKIE}=1`);
}

/**
 * What: the mock route's path after /api.
 * Use when: dispatching.
 * Inputs: the route params promise.
 * Output: a path like "demo/personas".
 * Side effects: none.
 * Role: helper.
 */
async function subPath(ctx: { params: Promise<{ path: string[] }> }): Promise<string> {
  return (await ctx.params).path.join("/");
}

/**
 * What: GET handler (personas and me).
 * Use when: the browser GETs /api/* under `pnpm dev:mock`.
 * Inputs: request and route params.
 * Output: a Response.
 * Side effects: none.
 * Role: mock route.
 */
export async function GET(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const path = await subPath(ctx);
  if (!hasAccess(req)) return fail(401, "access_denied", "ACCESS_REQUIRED", "Enter the access code to continue.");
  if (path === "demo/personas") return Response.json(MOCK_PERSONAS);
  if (path === "me") return Response.json({ visitor: true, operator: false, demo_faults_enabled: false });
  return fail(404, "not_found", "NOT_FOUND", "Not found.");
}

/**
 * What: POST handler (access and sessions).
 * Use when: the browser POSTs /api/* under `pnpm dev:mock`.
 * Inputs: request and route params.
 * Output: a Response.
 * Side effects: sets the mock cookie on a good code.
 * Role: mock route.
 */
export async function POST(req: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const path = await subPath(ctx);
  if (path === "access") {
    const body = (await req.json().catch(() => ({}))) as { code?: unknown };
    if (body.code === MOCK_RATE_LIMITED_CODE) {
      return fail(429, "rate_limited", "ACCESS_ATTEMPTS", "Too many access attempts from this network.", 42);
    }
    if (body.code !== MOCK_ACCESS_CODE) {
      return fail(401, "access_denied", "ACCESS_DENIED", "That access code did not work. Check your invitation and try again.");
    }
    return new Response(null, { status: 204, headers: { "set-cookie": `${COOKIE}=1; Path=/; SameSite=Lax` } });
  }
  if (!hasAccess(req)) return fail(401, "access_denied", "ACCESS_REQUIRED", "Enter the access code to continue.");
  if (path === "sessions") {
    return Response.json({ session_id: crypto.randomUUID(), created_at: new Date().toISOString() }, { status: 201 });
  }
  return fail(404, "not_found", "NOT_FOUND", "Not found.");
}
