/**
 * What: demo data for the dev-only mock API and the unit tests (five personas, the mock access code).
 * Use when: tests fake GET /api/demo/personas, or `pnpm dev:mock` serves the mock route.
 * Inputs: none.
 * Output: MOCK_ACCESS_CODE, MOCK_PERSONAS and MOCK_PINS.
 * Side effects: none.
 * Role: test and dev fixture only. Never import this from app code: the PINs must reach the
 *   browser only from the API at runtime, and the bundle check (src/build-output.pins.check.ts)
 *   fails the build if any of these PINs appears in the production output.
 *   Values follow the approved Figma Landing page; lane A owns the real seeds
 *   (data/seeds/personas.yaml, doc 07 s2.2).
 */
import type { Persona } from "@contracts/contracts";

/**
 * What: the access code the dev mock accepts.
 * Use when: typing into the gate while running `pnpm dev:mock`, or in tests.
 * Inputs: none.
 * Output: a string.
 * Side effects: none.
 * Role: mock constant (the real code is DEMO_ACCESS_CODE on the agent).
 */
export const MOCK_ACCESS_CODE = "quill-demo-mock";

/**
 * What: the access code that makes the dev mock answer 429 ACCESS_ATTEMPTS.
 * Use when: showing the countdown state locally.
 * Inputs: none.
 * Output: a string.
 * Side effects: none.
 * Role: mock constant.
 */
export const MOCK_RATE_LIMITED_CODE = "too-many";

/**
 * What: five demo personas in the doc 07 r4 Persona shape.
 * Use when: the mock returns GET /api/demo/personas, or a test needs the list.
 * Inputs: none.
 * Output: a readonly Persona array.
 * Side effects: none.
 * Role: fixture data.
 */
export const MOCK_PERSONAS: readonly Persona[] = [
  {
    name: "Priya Shah",
    role: "billing_admin",
    tier: "pro",
    contract_status: "active",
    account_id: "QS-10423",
    pin: "4821",
    scenarios: ["S1", "S2", "S5"],
    description: "Refund a duplicate $96 charge by chat, follow up by voice, then try a failure.",
  },
  {
    name: "Dana Okafor",
    role: "owner",
    tier: "business",
    contract_status: "active",
    account_id: "QS-20871",
    pin: "6604",
    scenarios: ["S3"],
    description: "Request a Business refund that needs approval.",
  },
  {
    name: "Marcus Lee",
    role: "member",
    tier: "business",
    contract_status: "active",
    account_id: "QS-20871",
    pin: "1937",
    scenarios: ["S3"],
    description: "Ask for a refund your role cannot request.",
  },
  {
    name: "Sam Rivera",
    role: "owner",
    tier: "free",
    contract_status: "active",
    account_id: "QS-30512",
    pin: "2290",
    scenarios: ["S4"],
    description: "Ask something off topic, then something our docs do not cover.",
  },
  {
    name: "Omar Haddad",
    role: "billing_admin",
    tier: "pro",
    contract_status: "past_due",
    account_id: "QS-40219",
    pin: "5518",
    scenarios: ["rules"],
    description: "See a refund blocked while the account is past due.",
  },
];

/**
 * What: every PIN in MOCK_PERSONAS.
 * Use when: the bundle check greps the production build for leaked PINs.
 * Inputs: none.
 * Output: a string array.
 * Side effects: none.
 * Role: fixture data.
 */
export const MOCK_PINS: readonly string[] = MOCK_PERSONAS.map((p) => p.pin);
