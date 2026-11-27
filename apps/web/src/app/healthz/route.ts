/**
 * What: liveness route for the web container, `GET /healthz`.
 * Use when: the Docker HEALTHCHECK and the Compose healthcheck probe the web service.
 * Inputs: none.
 * Output: 200 JSON {"status":"ok"} with no caching.
 * Side effects: none.
 * Role: health endpoint; outside `/api` so Caddy's `/api/*` route to the agent never shadows it.
 */
/**
 * What: marks the route dynamic so it is never prerendered.
 * Use when: Next.js decides how to build /healthz.
 * Inputs: none.
 * Output: the string force-dynamic.
 * Side effects: none.
 * Role: route config.
 */
export const dynamic = "force-dynamic";

/**
 * What: GET handler.
 * Use when: see module.
 * Inputs: none.
 * Output: Response.
 * Side effects: none.
 * Role: health endpoint.
 */
export function GET(): Response {
  return Response.json({ status: "ok" }, { headers: { "cache-control": "no-store" } });
}
