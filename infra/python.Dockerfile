# What: shared image for the three Python services and their migrate jobs (build arg SERVICE = agent | core | knowledge).
# Use when: Compose builds agent_a, agent_b, core, knowledge and the migrate_* jobs (context = repo root).
# Inputs: packages/contracts, services/<SERVICE>/pyproject.toml, uv.lock, src/, migrations/; infra/migrate.py.
# Output: image with deps in /app/.venv, source in /app/src, runner at /app/migrate.py.
# Side effects: none at build beyond layers. Role: one Dockerfile so the three services build the same way (D-029).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ARG SERVICE
ENV SERVICE=${SERVICE} \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src PYTHONUNBUFFERED=1
# Dependencies resolve in /build/services/<svc> so the relative path ../../packages/contracts exists; the venv is /app/.venv.
COPY packages/contracts /build/packages/contracts
WORKDIR /build/services/${SERVICE}
COPY services/${SERVICE}/pyproject.toml services/${SERVICE}/uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-dev --no-install-project
WORKDIR /app
COPY services/${SERVICE}/src ./src
COPY services/${SERVICE}/migrations ./migrations
COPY infra/migrate.py ./migrate.py
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
