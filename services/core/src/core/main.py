"""What: minimal stub FastAPI app for the core service exposing health endpoints only.
Use when: Compose needs a healthy core container before the real service exists.
Inputs: none. Output: ASGI app `app`. Side effects: none.
Role: placeholder from CS-E-01.S3.T1; replaced by the real service, keep the health paths."""
from fastapi import FastAPI

app = FastAPI(title="quillstack-core (stub)")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """What: liveness probe. Use when: a caller checks the process is up.
    Inputs: none. Output: {"status": "alive"}. Side effects: none. Role: Compose healthcheck target."""
    return {"status": "alive"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """What: readiness probe (stub: always ready, no dependency checks yet).
    Use when: Compose healthcheck and Caddy health checks. Inputs: none.
    Output: {"status": "stub"}. Side effects: none. Role: becomes the real readyz of doc 07 later."""
    return {"status": "stub"}
