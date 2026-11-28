# What: multi-stage image for the Quillstack web app (Next.js standalone output).
# Use when: `docker build -f apps/web/Dockerfile .` from the repo root (Compose: context ".", dockerfile "apps/web/Dockerfile").
# Inputs: apps/web sources and lockfile; build context is the repo root so packages/contracts/ts is copied in.
# Output: an image that serves the app on port 3000 as the non-root user "nextjs", with a HEALTHCHECK on /healthz.
# Side effects: none at runtime beyond serving HTTP; no secrets are baked in (web holds none, doc 07 s1.4).
# Role: lane C container for the `web` Compose service.

ARG NODE_IMAGE=node:22-alpine

FROM ${NODE_IMAGE} AS deps
WORKDIR /repo/apps/web
RUN corepack enable
COPY apps/web/package.json apps/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

FROM ${NODE_IMAGE} AS build
WORKDIR /repo/apps/web
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
COPY --from=deps /repo/apps/web/node_modules ./node_modules
COPY apps/web/ ./
# The app imports the generated contract types through the @contracts/* path alias.
COPY packages/contracts/ts/ /repo/packages/contracts/ts/
RUN pnpm build

FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs
# The standalone tree mirrors the tracing root (repo root), so server.js sits under apps/web.
COPY --from=build --chown=nextjs:nodejs /repo/apps/web/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /repo/apps/web/.next/static ./apps/web/.next/static
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "apps/web/server.js"]
