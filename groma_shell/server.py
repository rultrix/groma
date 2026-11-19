"""Flask in a background thread, so the shell can own the process lifetime."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from werkzeug.serving import make_server

from web.app import create_app


def _port_is_free(host: str, port: int) -> bool:
    if port == 0:
        return True
    probe = socket.socket()
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class LocalServer:
    def __init__(self, unit_root: Path, host: str = "127.0.0.1", port: int = 0, on_switch=None):
        self.app = create_app(unit_root, on_switch=on_switch)
        chosen = port if _port_is_free(host, port) else 0
        try:
            self._server = make_server(host, chosen, self.app, threaded=True)
        except (OSError, SystemExit):
            # werkzeug calls sys.exit(1) when the bind fails; fall back anyway.
            self._server = make_server(host, 0, self.app, threaded=True)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "LocalServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
