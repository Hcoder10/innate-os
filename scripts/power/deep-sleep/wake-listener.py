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
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 4022
DEEP_SLEEP = "/usr/local/sbin/innate-deep-sleep"

# USB hubs whose VBUS we cut during deep sleep (~0.06 A / ~0.7 W at the
# battery, measured): 1-2.3 carries the lidar board + 3D camera, its parent
# 1-2 carries the Arducam and the child hub itself. Both are ppps-capable
# (verified with uhubctl); WiFi/BT and the arm's serial are on root ports,
# untouched. Child before parent — the child is unreachable once the parent
# port is off. Hub power state resets on reboot, so this must run at
# deep-sleep boot (here) and the wake reboot restores it for free.
USB_HUBS_TO_CUT = ("1-2.3", "1-2")


def _cut_usb_peripherals() -> None:
    """Best-effort: a missing uhubctl or a failed cut must not break waking."""
    for hub in USB_HUBS_TO_CUT:
        for attempt in range(3):  # USB may still be enumerating right after boot
            try:
                result = subprocess.run(
                    ["uhubctl", "-l", hub, "-a", "off"], capture_output=True, text=True, timeout=15
                )
            except (OSError, subprocess.SubprocessError) as e:
                print(f"WARN: uhubctl {hub} failed: {e}")
                break
            if result.returncode == 0:
                print(f"USB hub {hub}: ports powered off")
                break
            if attempt < 2:
                time.sleep(3.0)
            else:
                print(f"WARN: uhubctl {hub} off failed: {result.stderr.strip()}")


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
    _cut_usb_peripherals()
    HTTPServer(("0.0.0.0", PORT), WakeHandler).serve_forever()
