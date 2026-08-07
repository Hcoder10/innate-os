# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Persistent per-map spatial memory: a JSON index plus one JPEG per memory.

Lives under ``data/spatial_memory/<map>/`` beside the maps it describes and
survives restarts. Memories only mean anything in the coordinate frame of the
map they were recorded on, so each map gets its own directory, and the index
carries a fingerprint of the map file — re-mapping under the same name yields
a new frame, and the stale memories are wiped rather than trusted.

Thread contract: mutations come from the recorder's timer (executor thread);
:meth:`snapshot` serves readers on the agent loop and search threads. Every
index access takes the store lock; image files are read without it, so a
concurrently evicted file reads as missing — callers tolerate that.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock

_INDEX_VERSION = 1


@dataclass(frozen=True)
class Memory:
    """One remembered viewpoint: where the robot stood and when it looked."""

    id: int
    x: float
    y: float
    theta: float
    stamp: float  # epoch seconds at capture


@dataclass(frozen=True)
class MemorySnapshot:
    """An immutable view of the store for readers on other threads."""

    map_name: str | None
    revision: int
    memories: tuple[Memory, ...]


class MemoryStore:
    def __init__(self, data_dir: Path):
        self._maps_dir = data_dir / "maps"
        self._root = data_dir / "spatial_memory"
        self._lock = Lock()
        self._map_name: str | None = None
        self._dir: Path | None = None
        self._memories: list[Memory] = []
        self._next_id = 1
        self._fingerprint = ""
        self._revision = 0
        self.last_change_monotonic = 0.0

    def switch_map(self, map_name: str | None) -> None:
        """Point the store at a map's memories, loading them from disk.

        Idempotent per name — the fingerprint check (file I/O) runs only when
        the name actually changes, so calling this every tick is free.
        """
        if map_name == self._map_name:
            return
        fingerprint = _map_fingerprint(self._maps_dir, map_name) if map_name else ""
        with self._lock:
            self._map_name = map_name
            self._dir = self._root / Path(map_name).stem if map_name else None
            self._memories = []
            self._next_id = 1
            self._fingerprint = fingerprint
            self._revision += 1
            self.last_change_monotonic = time.monotonic()
            if self._dir is not None:
                self._load_locked()

    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            return MemorySnapshot(self._map_name, self._revision, tuple(self._memories))

    def positions(self) -> list[dict]:
        """The webapp mirror payload: one ``{id, x, y, theta, stamp}`` per memory."""
        with self._lock:
            return [{"id": m.id, "x": m.x, "y": m.y, "theta": m.theta, "stamp": m.stamp} for m in self._memories]

    def image_path(self, memory_id: int) -> Path | None:
        with self._lock:
            return self._dir / f"{memory_id}.jpg" if self._dir is not None else None

    def files_index_path(self) -> Path | None:
        """Where the current map's server-side-upload registry lives (brain/frame_files.py)."""
        with self._lock:
            return self._dir / "files.json" if self._dir is not None else None

    def add(self, x: float, y: float, theta: float, stamp: float, jpeg: bytes) -> Memory | None:
        """Record a new memory; None when no map is loaded."""
        with self._lock:
            if self._dir is None:
                return None
            memory = Memory(self._next_id, x, y, theta, stamp)
            self._next_id += 1
            self._write_image_locked(memory.id, jpeg)
            self._memories.append(memory)
            self._commit_locked()
            return memory

    def replace(self, old: Memory, x: float, y: float, theta: float, stamp: float, jpeg: bytes) -> None:
        """Overwrite a memory in place — same slot, fresh view of the same spot."""
        with self._lock:
            if self._dir is None or all(m.id != old.id for m in self._memories):
                return
            memory = Memory(old.id, x, y, theta, stamp)
            self._write_image_locked(memory.id, jpeg)
            self._memories = [memory if m.id == old.id else m for m in self._memories]
            self._commit_locked()

    def evict(self, memory: Memory) -> None:
        with self._lock:
            if self._dir is None or all(m.id != memory.id for m in self._memories):
                return
            self._memories = [m for m in self._memories if m.id != memory.id]
            (self._dir / f"{memory.id}.jpg").unlink(missing_ok=True)
            self._commit_locked()

    # --- locked internals ---
    def _load_locked(self) -> None:
        assert self._dir is not None
        try:
            index = json.loads((self._dir / "index.json").read_text())
            stale = index.get("version") != _INDEX_VERSION or index.get("fingerprint") != self._fingerprint
        except (OSError, json.JSONDecodeError, TypeError):
            index, stale = {}, True
        if stale:
            self._wipe_locked()
            return
        self._memories = [Memory(**entry) for entry in index.get("memories", [])]
        self._next_id = int(index.get("next_id", 1))

    def _wipe_locked(self) -> None:
        """The map was re-made (or the index is unreadable): the coordinates lie."""
        assert self._dir is not None
        if self._dir.is_dir():
            for stale in self._dir.glob("*.jpg"):
                stale.unlink(missing_ok=True)
            (self._dir / "index.json").unlink(missing_ok=True)
            (self._dir / "files.json").unlink(missing_ok=True)
        self._memories = []
        self._next_id = 1

    def _write_image_locked(self, memory_id: int, jpeg: bytes) -> None:
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{memory_id}.jpg").write_bytes(jpeg)

    def _commit_locked(self) -> None:
        assert self._dir is not None
        index = {
            "version": _INDEX_VERSION,
            "map": self._map_name,
            "fingerprint": self._fingerprint,
            "next_id": self._next_id,
            "memories": [asdict(m) for m in self._memories],
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / "index.json.tmp"
        tmp.write_text(json.dumps(index))
        os.replace(tmp, self._dir / "index.json")
        self._revision += 1
        self.last_change_monotonic = time.monotonic()


def _map_fingerprint(maps_dir: Path, map_name: str) -> str:
    """Identity of the map's content, not its name — a re-mapped room must not match."""
    pgm = maps_dir / (Path(map_name).stem + ".pgm")
    source = pgm if pgm.exists() else maps_dir / map_name
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        return ""
