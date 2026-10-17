"""Run the simulator as an HTTP server the real `alva` CLI can talk to.

    ALVA_ENDPOINT=http://127.0.0.1:8787 ALVA_API_KEY=<scenario api_key> alva broker describe
"""

from __future__ import annotations

import signal
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import App, Request


def make_handler(app: App, lock: threading.Lock, verbose: bool = False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "gateway"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet unless asked
            if verbose:
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            req = Request.from_parts(self.command, self.path, dict(self.headers.items()), body)
            with lock:
                resp = app.handle(req)
            if resp.delay_ms:
                time.sleep(resp.delay_ms / 1000)
            if resp.action == "drop":
                self._drop()
                return
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if resp.body:
                self.wfile.write(resp.body)

        def _drop(self):
            """Close the TCP connection with a reset and no HTTP response."""
            self.close_connection = True
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close()
            except OSError:
                pass

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    return Handler


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128  # the default 5 resets connections under a burst of parallel calls

    def handle_error(self, request, client_address):
        # Dropped connections raise inside the handler on purpose.
        exc = sys.exc_info()[1]
        if isinstance(exc, (OSError, ValueError)):
            return
        super().handle_error(request, client_address)


def serve(sim, host: str = "127.0.0.1", port: int = 8787, control_token: str | None = None, verbose: bool = False):
    app = App(sim, control_token)
    lock = threading.Lock()
    httpd = QuietServer((host, port), make_handler(app, lock, verbose))

    def shutdown(*_):
        with lock:
            sim.rec.write_state(dict(sim.state(), unknown_routes=app.unknown_routes))
            sim.rec.close()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    sys.stderr.write(
        f"listening on http://{host}:{httpd.server_address[1]}  scenario={sim.scenario.get('name')}  "
        f"now={sim.scenario['clock']['start']}\n"
    )
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return app
