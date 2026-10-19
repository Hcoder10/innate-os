"""The shim injects the developer's key, so it must not relay for the LAN.

It has to listen on every interface -- the container reaches it through
host.docker.internal, never loopback -- and run_eval.sh starts it in the
background on any host. Before this, a LAN neighbour who found port 8099 could
make billable requests with the key and read the forwarded files/cache
endpoints. Found by an adversarial review.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import ipaddress
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import gemini_shim
import pytest


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.53", "::1", "192.168.65.3"])
def test_loopback_and_docker_desktop_are_trusted(ip: str) -> None:
    assert gemini_shim.trusted(ip)


# 172.16/12 is Docker's default pool, but it is not Docker's alone: a LAN or
# VPN can use it, and so does WSL's own NAT. Trusting all of it let a client
# at 172.20.1.10 bill the key (found by review).
@pytest.mark.parametrize(
    "ip",
    ["192.168.1.20", "10.0.0.5", "8.8.8.8", "172.32.0.1", "172.20.1.10", "172.25.107.75", "172.17.0.2"]
    + ["::ffff:172.18.0.5", "2001:db8::1", "not-an-ip", ""],
)
def test_the_lan_and_the_internet_are_not(ip: str) -> None:
    assert not gemini_shim.trusted(ip)


_IP_OUTPUT = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 172.25.107.75/20 brd 172.25.111.255 scope global eth0\\       valid_lft forever
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever
7: br-3f2a91c0d4e1    inet 172.18.0.1/16 brd 172.18.255.255 scope global br-3f2a91c0d4e1\\       valid_lft
"""


def test_only_the_hosts_own_docker_bridges_are_added() -> None:
    docker = gemini_shim.docker_networks(_IP_OUTPUT)
    assert docker == (ipaddress.ip_network("172.17.0.0/16"), ipaddress.ip_network("172.18.0.0/16"))
    networks = gemini_shim.allowed_networks("", docker)
    assert gemini_shim.trusted("172.17.0.2", networks)
    assert gemini_shim.trusted("::ffff:172.18.0.5", networks)
    assert not gemini_shim.trusted("172.25.107.75", networks)  # eth0 is the LAN side
    assert not gemini_shim.trusted("172.20.1.10", networks)


def test_extra_networks_come_from_the_environment_variable() -> None:
    networks = gemini_shim.allowed_networks("10.42.0.0/16, 192.168.1.0/24")
    assert gemini_shim.trusted("10.42.3.4", networks)
    assert gemini_shim.trusted("192.168.1.20", networks)
    assert not gemini_shim.trusted("10.43.0.1", networks)
    assert gemini_shim.allowed_networks("") == gemini_shim.TRUSTED_NETWORKS
    with pytest.raises(ValueError):
        gemini_shim.allowed_networks("not-a-network")


def test_an_untrusted_client_is_refused_before_anything_goes_upstream() -> None:
    """The real handler, with loopback removed from its trusted set so the
    test's own connection is the intruder. Upstream is the discard port: had
    the request been relayed, the answer would be a 502, not a 403."""
    saved = gemini_shim.Handler.networks, gemini_shim.UPSTREAM, gemini_shim.Handler.api_key
    saved_at = gemini_shim.Handler.refreshed_at
    server = ThreadingHTTPServer(("127.0.0.1", 0), gemini_shim.Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        gemini_shim.Handler.networks = (ipaddress.ip_network("192.0.2.0/24"),)  # TEST-NET-1: nobody
        gemini_shim.Handler.refreshed_at = time.monotonic()  # no re-read: it would restore loopback
        gemini_shim.UPSTREAM = "http://127.0.0.1:9"
        gemini_shim.Handler.api_key = "test-key-not-real"
        serving.start()
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1beta/models?pageSize=1", timeout=10)
        assert refused.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        gemini_shim.Handler.networks, gemini_shim.UPSTREAM, gemini_shim.Handler.api_key = saved
        gemini_shim.Handler.refreshed_at = saved_at


def test_a_bridge_created_after_startup_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_eval.sh starts the shim before `innate_up.sh` recreates the
    compose network: the container's bridge may not exist yet at startup."""
    h = gemini_shim.Handler
    monkeypatch.setattr(h, "networks", gemini_shim.TRUSTED_NETWORKS)
    monkeypatch.setattr(h, "refreshed_at", float("-inf"))
    monkeypatch.setattr(gemini_shim, "docker_networks", lambda: (ipaddress.ip_network("172.18.0.0/16"),))
    assert h._refreshed_trusts("172.18.0.5")
    assert gemini_shim.trusted("172.18.0.5", h.networks)


def test_refreshing_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    h = gemini_shim.Handler
    monkeypatch.setattr(h, "networks", gemini_shim.TRUSTED_NETWORKS)
    monkeypatch.setattr(h, "refreshed_at", float("-inf"))
    monkeypatch.setattr(gemini_shim, "docker_networks", lambda: calls.append(1) or ())
    for _ in range(5):
        assert not h._refreshed_trusts("192.168.1.20")
    assert len(calls) == 1


def test_a_routers_bridge_is_not_dockers() -> None:
    out = "4: br-lan    inet 192.168.1.1/24 brd 192.168.1.255 scope global br-lan\\       valid_lft forever\n"
    assert gemini_shim.docker_networks(out) == ()
