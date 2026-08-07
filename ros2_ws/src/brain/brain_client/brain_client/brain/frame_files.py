# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Server-side copies of the remembered frames — the Gemini Files API tier.

Each recorded JPEG is uploaded once, in the background, and referenced in
requests as ``fileData``: a frame's bytes cross the network at record time and
never again. The registry is a sidecar beside the store's images, so a restart
re-uploads nothing; an entry is trusted only while its stamp matches the live
memory, so a wiped-and-recreated id can never resolve to an old upload. Files
expire server-side after 48 h — :meth:`maintain` (driven by the recorder's
1 Hz tick via ``MemorySearch.warm``) re-uploads well before that.

The whole tier is a pure optimization: a missing, expired, or failed upload
just means that frame rides ``inlineData`` in the next request, and a backend
without the upload endpoint latches the tier off for the process.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.brain.transport import FILES_UPLOAD_PATH, UNSUPPORTED_ENDPOINT_STATUSES, GeminiHttpError

if TYPE_CHECKING:
    from pathlib import Path

    from brain_client.brain.transport import GeminiRest
    from brain_client.memory.store import Memory, MemoryStore

_FILE_LIFETIME_SEC = 48 * 3600  # Gemini deletes uploads after this
_USABLE_FOR_SEC = 47 * 3600  # stop referencing this long after upload (margin under the deletion)
_REUPLOAD_AFTER_SEC = 40 * 3600  # the background chore refreshes an upload past this age


@dataclass(frozen=True)
class _FileRecord:
    uri: str
    uploaded_at: float  # epoch seconds
    image_stamp: float  # the memory's stamp at upload — a mismatch means the frame changed


class FrameFiles:
    def __init__(self, store: MemoryStore, rest: GeminiRest, logger):
        self._store = store
        self._rest = rest
        self._logger = logger
        self.unsupported = False  # latched: the backend has no upload passthrough
        self._lock = threading.Lock()  # registry map + sidecar IO
        self._busy = threading.Lock()  # at most one maintenance thread
        self._dir: Path | None = None  # which map's registry is loaded
        self._records: dict[int, _FileRecord] = {}

    def uri_for(self, memory: Memory) -> str | None:
        """A live server-side URI for exactly this frame, or None (ride inline)."""
        with self._lock:
            self._sync_map_locked()
            record = self._records.get(memory.id)
        if record is None or record.image_stamp != memory.stamp:
            return None
        if time.time() - record.uploaded_at > _USABLE_FOR_SEC:
            return None
        return record.uri

    def maintain(self) -> None:
        """Upload new/refreshed/aging frames in the background; returns immediately."""
        if self.unsupported:
            return
        pending = self._pending()
        if not pending:
            return
        if not self._busy.acquire(blocking=False):
            return  # a maintenance round is already running

        def run() -> None:
            try:
                self._upload(pending)
            finally:
                self._busy.release()

        threading.Thread(target=run, name="memory-file-upload", daemon=True).start()

    def _pending(self) -> list[Memory]:
        snapshot = self._store.snapshot()
        now = time.time()
        with self._lock:
            self._sync_map_locked()
            if self._dir is None:
                return []
            records = dict(self._records)
        pending = []
        for memory in snapshot.memories:
            record = records.get(memory.id)
            if record is None or record.image_stamp != memory.stamp or now - record.uploaded_at > _REUPLOAD_AFTER_SEC:
                pending.append(memory)
        return pending

    def _upload(self, pending: list[Memory]) -> None:
        """One maintenance round. A transient failure ends the round (the next
        tick retries); an unsupported-endpoint answer latches the tier off."""
        uploaded_into = self._dir  # a map switch mid-round orphans these uploads
        for memory in pending:
            path = self._store.image_path(memory.id)
            if path is None:
                return
            try:
                data = path.read_bytes()
            except OSError:
                continue  # evicted between the scan and now
            try:
                response = self._rest.upload(FILES_UPLOAD_PATH, data, "image/jpeg")
            except GeminiHttpError as error:
                if error.status in UNSUPPORTED_ENDPOINT_STATUSES:
                    self.unsupported = True
                    self._logger.warn(
                        f"[Memory] backend has no file-upload endpoint (HTTP {error.status}); frames ride inline"
                    )
                else:
                    self._logger.warn(f"[Memory] frame upload failed ({error}); retrying next tick")
                return
            except Exception as error:  # noqa: BLE001 — network trouble ends the round, never the process
                self._logger.warn(f"[Memory] frame upload failed ({error!r}); retrying next tick")
                return
            uri = str((response.get("file") or {}).get("uri") or "")
            if not uri:
                self._logger.warn("[Memory] frame upload returned no file uri; retrying next tick")
                return
            with self._lock:
                self._sync_map_locked()
                if self._dir != uploaded_into:
                    return  # the map switched mid-round; these frames are gone
                self._records[memory.id] = _FileRecord(uri=uri, uploaded_at=time.time(), image_stamp=memory.stamp)
                self._save_locked()

    # --- registry persistence (under self._lock) ---
    def _sync_map_locked(self) -> None:
        path = self._store.files_index_path()
        target = path.parent if path is not None else None
        if target == self._dir:
            return
        self._dir = target
        self._records = self._load(path) if path is not None else {}

    def _load(self, path: Path) -> dict[int, _FileRecord]:
        try:
            data = json.loads(path.read_text())
            return {
                int(memory_id): _FileRecord(
                    uri=str(entry["uri"]),
                    uploaded_at=float(entry["uploaded_at"]),
                    image_stamp=float(entry["image_stamp"]),
                )
                for memory_id, entry in data.get("files", {}).items()
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return {}  # unreadable registry = nothing uploaded; the chore rebuilds it

    def _save_locked(self) -> None:
        path = self._store.files_index_path()
        if path is None:
            return
        live_ids = {memory.id for memory in self._store.snapshot().memories}
        self._records = {memory_id: record for memory_id, record in self._records.items() if memory_id in live_ids}
        index = {
            "version": 1,
            "files": {
                str(memory_id): {
                    "uri": record.uri,
                    "uploaded_at": record.uploaded_at,
                    "image_stamp": record.image_stamp,
                }
                for memory_id, record in self._records.items()
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(index))
        os.replace(tmp, path)
