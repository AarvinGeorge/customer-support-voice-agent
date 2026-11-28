# CS-E-07.F1: no LangSmith link in the product UI (D-087)

Model: claude-sonnet-5-5. Branch `cs-e-07-f1-no-langsmith-link`.

## Why
Reviewers cannot open the maintainer's private LangSmith project, and a link to an operator tool, with the project URL baked into a public bundle, does not belong in a customer-facing page (brief coverage audit gap 3).

## Changed
- Drawer footer: "Open in LangSmith" link and the "LangSmith link not configured" text removed. Steps, gates, claims, timings, copy-trace-id button and the plain-text `trace xxxxxxxx` are unchanged.
- Removed `langsmithRunUrl` (lib/turns/inspector.ts), the `langsmithProjectUrl` and `langsmithProject` props, and `lib/config/public.ts` (its only users).
- Build plumbing removed: `ARG`/`ENV NEXT_PUBLIC_LANGSMITH_*` in apps/web/Dockerfile, `build.args` of `web` in compose.yaml, the "Require the web image's public build arguments" step and the bake env in images.yml, `LANGSMITH_PROJECT_URL` in .env.example, DEPLOY.md section 0 and the build-time paragraph. Repository variable LANGSMITH_PROJECT_URL is no longer needed (LANGSMITH_PROJECT is still used by the backend).
- Backend LangSmith tracing and services/agent untouched.

## Red first
1. Drawer test (injected project URL) failed: link found (1 failed, 11 passed).
2. New `src/build-output.no-langsmith.check.ts`, built with `NEXT_PUBLIC_LANGSMITH_PROJECT_URL=https://smith.langchain.com/o/0000/projects/p/1111`: failed, hits in `static/chunks/1jkc6i4gkc7m0.js`, `server/chunks/ssr/apps_web_*.js(.map)` and the standalone copy. The check has a positive control for its matcher.

## Green (same build env var still set)
- `pnpm test`: 28 files, 424 tests passed. `pnpm lint` and `pnpm typecheck` clean.
- `pnpm build` then `pnpm test:bundle`: 2 files, 6 tests passed.
- `grep -rIl "smith.langchain" apps/web/.next --exclude-dir=cache | wc -l` printed `0` (was 4 files before).
- `scripts/check_descriptors.py`: 6 files, 0 gaps. `pytest scripts/test_deploy.py tests/test_readme_posture.py`: 63 passed (the old build-argument guard test is replaced by `test_web_image_bakes_no_langsmith_value`).

## Docs
README observability section and DESIGN.md line updated. No em dashes. Real API calls: 0.
