"""What: ASGI entry point of the core service (uvicorn core.main:app).
Use when: Compose starts the core container. Inputs: environment (see core.config). Output: ASGI app `app`.
Side effects: reads the environment at import; raises ConfigError if a token or CORE_DB_URL is missing.
Role: replaces the CS-E-01.S3.T1 stub; the health paths /healthz and /readyz are unchanged."""
from core.app import create_app
from core.config import Settings

app = create_app(Settings.from_env())
