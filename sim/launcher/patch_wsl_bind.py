#!/usr/bin/env python3
"""Make the host world server bindable under Docker Desktop's WSL integration.

THE BUG, upstream in the launcher rather than in the benchmark.

_world_server_binds() asks Docker for the default bridge gateway and binds it,
on the reasoning stated in its own docstring: macOS reaches the host via
host.docker.internal so loopback suffices, while "a native Linux/WSL engine
resolves it to the default bridge gateway instead -- bind that too".

That splits the world in two where there are three. Docker Desktop on Windows
with WSL2 integration is neither case: the engine and every container run in
the `docker-desktop` distro, while the launcher runs in `Ubuntu`. So
`docker network inspect bridge` truthfully reports 172.17.0.1 -- the gateway
inside the OTHER distro's network namespace -- and binding it from Ubuntu fails
with EADDRNOTAVAIL. There is no docker0 interface here at all.

The symptom is badly misleading, which is why this took a while: the bind
happens after the GL self-test in the same subprocess, so the launcher's
backend ladder reported

    Error: No working rendering backend for the sim world
    (tried native GL, EGL offscreen, software (OSMesa))

and told me to install four libraries that were already installed. The log
shows the truth one line above the traceback:

    [world-server] GL self-test (osmesa): 21 ms/frame (first frame 550 ms)
    OSError: [Errno 99] Cannot assign requested address ('172.17.0.1', 8799)

Rendering was fine every time.

THE FIX. Only bind an address this machine can actually assign, and when the
reported gateway is not one of ours, fall back to the address containers can
actually reach us on -- this distro's own interface address. Under Docker
Desktop that is a host-only virtual switch (172.x, not LAN-routable), which is
the same safety property the gateway bind relied on, so nothing is exposed
beyond the host and its containers.

Idempotent.
"""

from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "runtime.py"
MARKER = "_assignable_here"

OLD = '''    if sys.platform == "darwin":
        return "127.0.0.1"
    gateway = capture_command_output(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        timeout=DOCKER_PROBE_TIMEOUT_S,
    )
    parts = gateway.split(".")
    if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return f"127.0.0.1,{gateway}"
    return ""'''

NEW = '''    if sys.platform == "darwin":
        return "127.0.0.1"
    gateway = capture_command_output(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        timeout=DOCKER_PROBE_TIMEOUT_S,
    )
    parts = gateway.split(".")
    if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts):
        # ...but only if this machine can actually assign it. Under Docker
        # Desktop's WSL2 integration the engine and every container live in the
        # `docker-desktop` distro while the launcher runs in another one, so
        # `docker network inspect` truthfully reports a gateway that belongs to
        # a different network namespace. Binding it fails EADDRNOTAVAIL, and
        # because the bind happens after the GL self-test in the same
        # subprocess, the backend ladder blames rendering and tells you to
        # install libraries you already have. See patch_wsl_bind.py.
        if _assignable_here(gateway):
            return f"127.0.0.1,{gateway}"
        local = _own_interface_address()
        if local:
            # The address containers can actually reach this distro on. Under
            # Docker Desktop that is a host-only virtual switch (172.x, not
            # LAN-routable) -- the same safety property the gateway bind
            # relied on, so nothing is exposed beyond the host.
            return f"127.0.0.1,{local}"
    return ""


def _assignable_here(addr: str) -> bool:
    """Whether this machine can bind `addr` at all. Asks the kernel rather than
    inferring from platform or interface names."""
    import socket as _socket

    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            s.bind((addr, 0))
        return True
    except OSError:
        return False


def _own_interface_address() -> str:
    """This host's primary non-loopback IPv4, or "". No traffic is sent: a
    connected UDP socket only asks the routing table which source address
    would be used."""
    import socket as _socket

    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: reserved, never routed
            addr = s.getsockname()[0]
        return addr if addr and not addr.startswith("127.") else ""
    except OSError:
        return ""'''


def main() -> int:
    text = TARGET.read_text()
    if MARKER in text:
        print("already patched")
        return 0
    if text.count(OLD) != 1:
        print(f"FAILED: anchor appears {text.count(OLD)} times")
        return 1
    TARGET.write_text(text.replace(OLD, NEW))
    print("patched sim/launcher/runtime.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
