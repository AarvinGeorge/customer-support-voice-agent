/**
 * What: TEMPORARY local copy of the doc 07 s1.3 error envelope types the API client needs.
 * Use when: importing ErrorBody, ErrorEnvelope or ErrorClass inside apps/web until the
 *   contracts package merges.
 * Inputs: none.
 * Output: type declarations plus the ERROR_CLASSES runtime list used to validate bodies.
 * Side effects: none.
 * Role: stand-in for the generated contract types. Names and fields mirror
 *   packages/contracts/ts/contracts.ts on branch cs-e-01-s6-contracts exactly.
 *
 * TODO(CS-E-01.S6 merge): delete this file and replace imports with
 *   `import type { ErrorBody, ErrorEnvelope } from "../../../../../packages/contracts/ts/contracts";`
 *   (or a `@contracts/*` tsconfig path to packages/contracts/ts). Move ERROR_CLASSES
 *   into client.ts, typed `satisfies readonly ErrorBody["class"][]`, so a class
 *   removed from the contract fails the type check.
 */

/** What: the error classes of doc 07 s1.3. Role: runtime validation list and type source. */
export const ERROR_CLASSES = [
  "provider_timeout",
  "provider_rate_limited",
  "provider_spend_cap",
  "provider_5xx",
  "provider_auth",
  "retrieval_failed",
  "core_client_error",
  "core_conflict",
  "core_unavailable",
  "db_unavailable",
  "redis_unavailable",
  "rate_limited",
  "admission_rejected",
  "turn_deadline_exceeded",
  "access_denied",
  "not_found",
  "validation",
] as const;

/** What: one doc 07 s1.3 error class. Role: union type. */
export type ErrorClass = (typeof ERROR_CLASSES)[number];

/** What: doc 07 s1.3 ErrorBody (JSON key "class"). Role: wire type of every non-2xx body. */
export interface ErrorBody {
  class: ErrorClass;
  code: string;
  message: string;
  retryable: boolean;
  changed: boolean | null;
  retry_after_s?: number | null;
  fault_injected: boolean;
  trace_id?: string | null;
  details: Record<string, unknown>;
}

/** What: wrapper {"error": ErrorBody}. Role: wire type of every non-2xx response. */
export interface ErrorEnvelope {
  error: ErrorBody;
}
