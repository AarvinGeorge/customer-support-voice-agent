/**
 * What: the web app's public (non-secret) config values, read from NEXT_PUBLIC_* variables that
 *   Next.js inlines at build time.
 * Use when: a client component needs deploy config, such as the LangSmith project link.
 * Inputs: NEXT_PUBLIC_LANGSMITH_PROJECT (the LANGSMITH_PROJECT name, doc 07 s15.2) and
 *   NEXT_PUBLIC_LANGSMITH_PROJECT_URL (that project's LangSmith page,
 *   https://smith.langchain.com/o/<org id>/projects/p/<project id>; ids, not a key).
 * Output: a frozen object.
 * Side effects: none.
 * Role: config; `NEXT_PUBLIC_*` never holds a secret (doc 07 s15.2). The property accesses are
 *   literal so Next.js can inline them.
 */
export const publicConfig = Object.freeze({
  langsmithProject: process.env.NEXT_PUBLIC_LANGSMITH_PROJECT ?? "",
  langsmithProjectUrl: process.env.NEXT_PUBLIC_LANGSMITH_PROJECT_URL ?? "",
});
