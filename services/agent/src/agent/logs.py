"""What: the redacting log formatter (doc 07 s11: message text never in logs; PIN digits and emails redacted in
every log line as a net behind the code that logs ids only).
Use when: the production lifespan calls install_log_redaction once; tests format records with RedactingFormatter.
Inputs: log records. Output: RedactingFormatter, install_log_redaction, LOG_FORMAT.
Side effects: install_log_redaction adds a stderr handler to the root logger when it has none and sets the
formatter of every root handler.
Role: the "logs" sink of the s11.1 sink test.
"""
from __future__ import annotations

import logging

from contracts.redact import RedactContext, scan_text

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_LOG_CONTEXT = RedactContext()  # no session facts in a log line: unverified, nothing awaiting


class RedactingFormatter(logging.Formatter):
    """What: a Formatter whose output (message, arguments and traceback) goes through contracts scan_text.
    Use when: every root handler of the agent process. Inputs: format string. Output: redacted lines.
    Side effects: none. Role: log redaction net."""

    def format(self, record: logging.LogRecord) -> str:
        """What: format, then redact. Use when: logging. Inputs: record. Output: line. Side effects: none.
        Role: override."""
        try:
            return scan_text(super().format(record), _LOG_CONTEXT)
        except Exception:  # noqa: BLE001 - review round 2 N-I1: never let logging print the raw record
            return f"{record.levelname} {record.name} [log line not shown: redaction failed]"


def install_log_redaction(level: int = logging.INFO) -> None:
    """What: make every root handler use RedactingFormatter (adding a stderr handler when there is none, so the
    agent.* INFO lines are visible under uvicorn). Use when: the production lifespan, once.
    Inputs: level for the root logger when a handler is added. Output: none.
    Side effects: mutates the root logger. Role: wiring."""
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
        root.setLevel(level)
    for handler in root.handlers:
        handler.setFormatter(RedactingFormatter(LOG_FORMAT))
