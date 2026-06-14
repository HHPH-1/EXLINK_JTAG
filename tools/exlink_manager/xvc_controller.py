from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Callable

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from exlink_xvc_server import ExlinkXvcServer, XvcServerConfig, XvcStatus


class XvcController:
    def __init__(self, log_callback: Callable[[str], None] | None = None):
        self.log_callback = log_callback
        self._server: ExlinkXvcServer | None = None

    def start(self, config: XvcServerConfig, ready_timeout: float = 8.0) -> None:
        if self._server is not None and self._server.get_status().running:
            raise RuntimeError("XVC server is already running")
        self._server = ExlinkXvcServer(config, self.log_callback)
        self._server.start()
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            status = self._server.get_status()
            if status.listening:
                return
            if not status.running:
                raise RuntimeError(status.last_error or "XVC server stopped during startup")
            if status.last_error:
                raise RuntimeError(status.last_error)
            time.sleep(0.05)
        raise TimeoutError("timed out waiting for XVC server to listen")

    def stop(self, timeout: float = 5.0) -> None:
        if self._server is None:
            return
        self._server.stop()
        self._server.wait(timeout)

    def status(self) -> XvcStatus:
        if self._server is None:
            return XvcStatus()
        return self._server.get_status()

    @property
    def running(self) -> bool:
        return self.status().running
