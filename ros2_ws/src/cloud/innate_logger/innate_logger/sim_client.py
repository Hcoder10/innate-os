# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Usage telemetry transport for the simulator.

Differs from :mod:`innate_logger.client` in the three ways the sim differs
from a robot: authentication is optional (most sim users have no service
key), being offline is a normal steady state rather than a boot transient,
and no network call may ever run on the ROS executor.

Everything here therefore lives on one background thread: auth, HTTP, backoff
and the spool. Callers only ever touch the queue.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Backoff between failed flushes. Offline sims sit at the ceiling rather than
# retrying every heartbeat for hours.
_BACKOFF_START_S = 5.0
_BACKOFF_MAX_S = 300.0

# The spool holds events only. Heartbeats are dropped when offline: a stale
# "I was alive 3 days ago" adds nothing a gap in the series does not already
# show, and they would crowd out the events worth keeping.
_SPOOL_MAX_EVENTS = 2000
# One flush's worth, matching the server's per-request cap.
_SPOOL_BATCH = 500


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimAuth:
    """A bearer token for the telemetry endpoints, however it was obtained.

    Two sources, picked by whether the install has a service key:
    :class:`auth_client.AuthProvider` for keyed installs, or an anonymous
    token minted against the install id for everyone else. Both are refreshed
    lazily and only ever from the uploader thread.
    """

    def __init__(self, issuer_url: str, install_id: str, service_key: str = "") -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._install_id = install_id
        self._provider: Any | None = None
        self._anon_token: str | None = None
        # Anonymous tokens are re-minted a little early rather than on 401, so
        # a heartbeat is not spent discovering the expiry.
        self._anon_expires_at: float = 0.0

        if service_key:
            from auth_client import AuthProvider

            self._provider = AuthProvider(issuer_url=self._issuer_url, service_key=service_key)

    @property
    def anonymous(self) -> bool:
        return self._provider is None

    def token(self) -> str:
        """Return a usable bearer token, fetching one if needed.

        Raises whatever the underlying transport raises; the caller treats any
        failure as "still offline".
        """
        if self._provider is not None:
            return str(self._provider.token)

        if self._anon_token and time.monotonic() < self._anon_expires_at:
            return self._anon_token

        return self._mint_anonymous()

    def invalidate(self) -> None:
        """Force the next :meth:`token` call to fetch a fresh one."""
        if self._provider is not None:
            self._provider.token_needs_renewal = True
        else:
            self._anon_token = None
            self._anon_expires_at = 0.0

    def _mint_anonymous(self) -> str:
        request = Request(
            f"{self._issuer_url}/v1/sim/token",
            data=json.dumps({"install_id": self._install_id}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "innate-sim"},
            method="POST",
        )
        with urlopen(request, timeout=10.0) as response:
            body = json.loads(response.read().decode("utf-8"))

        self._anon_token = str(body["token"])
        # Well inside the issuer's expiry (10 min) without assuming its value.
        self._anon_expires_at = time.monotonic() + 240.0
        return self._anon_token


class EventSpool:
    """Events that have not reached the cloud yet, kept across restarts.

    Single-writer by construction — only the uploader thread touches the file
    — so it needs no locking. That is why challenge events arrive over a ROS
    topic instead of being written here by the world server, which is a
    different process on the other side of the container boundary.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pending: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                self._pending.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from a hard kill
        if self._pending:
            logger.info("Recovered %d spooled sim events", len(self._pending))

    def add(self, event: dict[str, Any]) -> None:
        self._pending.append(event)
        # Oldest out first: a full spool means a long offline stretch, and the
        # recent events describe what the user is doing now.
        if len(self._pending) > _SPOOL_MAX_EVENTS:
            del self._pending[: len(self._pending) - _SPOOL_MAX_EVENTS]
        self._persist()

    def batch(self) -> list[dict[str, Any]]:
        return self._pending[:_SPOOL_BATCH]

    def drop(self, count: int) -> None:
        del self._pending[:count]
        self._persist()

    def __len__(self) -> int:
        return len(self._pending)

    def _persist(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                "".join(json.dumps(e) + "\n" for e in self._pending), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("Could not persist sim spool: %s", exc)


class SimTelemetryUploader:
    """Owns the only thread allowed to do network I/O for sim telemetry."""

    def __init__(
        self,
        telemetry_url: str,
        auth: SimAuth,
        spool_path: Path,
        identity: dict[str, Any],
    ) -> None:
        self.base_url = telemetry_url.rstrip("/")
        self._auth = auth
        self._identity = identity
        self._spool = EventSpool(spool_path)
        self._heartbeats: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=4)
        self._new_events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._backoff = _BACKOFF_START_S
        self._online: bool | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="sim-telemetry")

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Ask the thread to make one last flush attempt, then give up.

        Bounded because this runs during node shutdown: an offline sim must not
        hang the exit waiting for a connection that will not happen.
        """
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)

    def heartbeat(self, vitals: dict[str, Any]) -> None:
        """Queue a heartbeat. Dropped rather than spooled when offline."""
        try:
            self._heartbeats.put_nowait({**self._identity, **vitals, "occurred_at": _utc_now_iso()})
        except queue.Full:
            pass  # the uploader is behind; the next tick supersedes this one anyway
        self._wake.set()

    def event(self, name: str, payload: dict[str, Any] | None = None, occurred_at: str | None = None) -> None:
        """Queue an event. Spooled if it cannot be sent now."""
        try:
            self._new_events.put_nowait(
                {
                    **self._identity,
                    "event": name,
                    "payload": payload or {},
                    "occurred_at": occurred_at or _utc_now_iso(),
                }
            )
        except queue.Full:
            logger.debug("Sim event queue full, dropping %s", name)
        self._wake.set()

    # ── Uploader thread ──────────────────────────────────────────────

    def _run(self) -> None:
        while True:
            stopping = self._stop.is_set()
            self._drain_new_events()

            sent_any = self._flush_events()
            sent_any = self._flush_heartbeats() or sent_any

            if stopping:
                return

            # Idle wait when healthy; backoff when the last attempt failed.
            wait = self._backoff if self._online is False else 1.0
            self._wake.wait(timeout=wait)
            self._wake.clear()
            del sent_any

    def _drain_new_events(self) -> None:
        while True:
            try:
                self._spool.add(self._new_events.get_nowait())
            except queue.Empty:
                return

    def _flush_events(self) -> bool:
        if not len(self._spool):
            return False
        batch = self._spool.batch()
        if not self._post("/log/sim/events", {"events": batch}):
            return False
        self._spool.drop(len(batch))
        return True

    def _flush_heartbeats(self) -> bool:
        sent = False
        while True:
            try:
                beat = self._heartbeats.get_nowait()
            except queue.Empty:
                return sent
            if not self._post("/log/sim/heartbeat", beat):
                return sent  # still offline; the rest are stale by now
            sent = True

    def _post(self, endpoint: str, payload: dict[str, Any]) -> bool:
        try:
            token = self._auth.token()
        except Exception as exc:  # noqa: BLE001 -- offline is ordinary here
            self._went_offline("auth unavailable: %s", exc)
            return False

        body = json.dumps(payload).encode("utf-8")
        for attempt in range(2):
            try:
                request = Request(
                    f"{self.base_url}{endpoint}",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "innate-sim",
                    },
                    method="POST",
                )
                with urlopen(request, timeout=10.0) as response:
                    if response.status == 200:
                        self._went_online()
                        return True
                    self._went_offline("telemetry returned HTTP %s", response.status)
                    return False
            except HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self._auth.invalidate()
                    try:
                        token = self._auth.token()
                    except Exception as auth_exc:  # noqa: BLE001
                        self._went_offline("auth unavailable: %s", auth_exc)
                        return False
                    continue
                # 429 is the server asking for quiet; the backoff already does that.
                self._went_offline("telemetry HTTP %d %s", exc.code, exc.reason)
                return False
            except (URLError, OSError) as exc:
                self._went_offline("telemetry unreachable: %s", exc)
                return False
        return False

    def _went_online(self) -> None:
        if self._online is not True:
            logger.info("Sim telemetry connected to %s", self.base_url)
        self._online = True
        self._backoff = _BACKOFF_START_S

    def _went_offline(self, message: str, *args: Any) -> None:
        # Debug, not warning: running the sim with no connectivity is a
        # supported mode, and an every-few-seconds warning reads to a user as
        # though the simulator itself is broken.
        logger.debug("Sim telemetry offline (" + message + ")", *args)
        if self._online is not False:
            logger.info("Sim telemetry offline — events will be sent when connectivity returns")
        self._online = False
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX_S)
