"""What: tiny stdlib server that replays a contracts JSONL fixture as Server-Sent Events, one event per delay.
Use when: proving the Caddy edge streams SSE unbuffered, before the real agent exists (CS-E-01.S3.T3).
Inputs: argv[1] = fixture path, env SERVED_BY (replica name, default agent_a), DELAY_S (default 0.5), PORT (default 8000).
Output: GET /api/readyz 200; POST or GET /api/stream answers text/event-stream with header X-Served-By.
Side effects: listens on PORT; none on disk.
Role: test tool only, never part of the product images."""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVED_BY = os.environ.get("SERVED_BY", "agent_a")
DELAY_S = float(os.environ.get("DELAY_S", "0.5"))
FIXTURE = sys.argv[1]


class Handler(BaseHTTPRequestHandler):
    """What: request handler for the fixture replay. Use when: ThreadingHTTPServer needs a handler.
    Inputs: HTTP requests. Output: readiness JSON or an SSE stream. Side effects: writes to the socket.
    Role: the whole fake agent."""

    protocol_version = "HTTP/1.1"

    def _readyz(self) -> None:
        """What: answer the readiness probe. Use when: path is /api/readyz. Inputs: none.
        Output: 200 JSON. Side effects: socket write. Role: lets Caddy health checks pass."""
        body = b'{"status":"stub"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        """What: replay the fixture, one SSE event per DELAY_S. Use when: path is /api/stream.
        Inputs: the fixture file. Output: `event:`/`data:` frames. Side effects: socket writes with sleeps.
        Role: the stream that curl -N must see arrive one by one."""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Served-By", SERVED_BY)
        self.send_header("Connection", "close")
        self.end_headers()
        with open(FIXTURE) as fh:
            for line in fh:
                event = json.loads(line)
                frame = f"id: {event['seq']}\nevent: {event['type']}\ndata: {line.strip()}\n\n"
                self.wfile.write(frame.encode())
                self.wfile.flush()
                time.sleep(DELAY_S)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        """What: route GET. Use when: any GET. Inputs: path. Output: readyz, stream or 404.
        Side effects: socket write. Role: router."""
        if self.path == "/api/readyz":
            self._readyz()
        elif self.path == "/api/stream":
            self._stream()
        else:
            self.send_error(404)

    do_POST = do_GET  # noqa: N815


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
