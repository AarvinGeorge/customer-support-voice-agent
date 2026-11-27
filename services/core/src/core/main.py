"""What: ASGI entry point of the core service (uvicorn core.main:app).
Use when: Compose starts the core container. Inputs: environment (see core.config). Output: ASGI app `app`.
Side effects: configures INFO logging (uvicorn leaves the root logger at WARNING) and reads the environment at import; raises ConfigError if a token or CORE_DB_URL is missing.
Role: replaces the CS-E-01.S3.T1 stub; the health paths /healthz and /readyz are unchanged."""
import logging

from core.app import create_app
from core.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app(Settings.from_env())
