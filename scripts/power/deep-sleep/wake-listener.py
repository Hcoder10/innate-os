#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Deep-sleep network wake listener.

The only thing (besides sshd) listening while the robot deep-sleeps in the
SLEEPNET nvpmodel mode. Stdlib-only, a few MB of RAM, happy on 2 cores at
115 MHz.

    GET  /      -> {"state": "deep_sleep", ...}
    POST /wake  -> restores full power + robot stack and reboots (~90 s to up)

Runs as root (systemd unit innate-deepsleep-wake.service) because waking
means nvpmodel + systemctl + reboot. Unauthenticated by design for the
experiment: waking is a benign action, and the port is LAN-only.
"""

import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 4022
DEEP_SLEEP = "/usr/local/sbin/innate-deep-sleep"


class WakeHandler(BaseHTTPRequestHandler):
    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, {"state": "deep_sleep", "wake": f"POST http://<robot>:{PORT}/wake"})

    def do_POST(self):
        if self.path != "/wake":
            self._reply(404, {"error": "unknown path"})
            return
        self._reply(200, {"ok": True, "message": "waking: restoring full power, rebooting"})
        # Reply is written before this reboots the machine.
        subprocess.Popen([DEEP_SLEEP, "wake"])

    def log_message(self, fmt, *args):
        pass  # keep journald quiet


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), WakeHandler).serve_forever()
