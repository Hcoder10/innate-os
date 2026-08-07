# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Spatial memory: admission policy, the on-disk store, the recorder's gates,
and the Gemini search with its context-cache lifecycle."""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace

import pytest

from brain_client.brain.memory_search import MemorySearch, verdict_text
from brain_client.brain.transport import CACHED_CONTENTS_PATH, GeminiHttpError, GeminiRest
from brain_client.memory import recorder as recorder_module
from brain_client.memory.recorder import MemoryRecorder
from brain_client.memory.selection import MAX_MEMORIES, plan_admission
from brain_client.memory.store import Memory, MemoryStore

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"


def memory(id_: int, x: float, y: float = 0.0, theta: float = 0.0, stamp: float = 1000.0) -> Memory:
    return Memory(id=id_, x=x, y=y, theta=theta, stamp=stamp)


# ================= admission policy =================


def test_first_viewpoint_is_recorded():
    plan = plan_admission([], 0.0, 0.0, 0.0, stamp=1000.0)
    assert plan.record and plan.replace is None and plan.evict is None


def test_near_duplicate_viewpoint_is_skipped():
    plan = plan_admission([memory(1, 0.0, 0.0)], 0.3, 0.2, 0.2, stamp=1010.0)
    assert not plan.record


def test_same_spot_facing_elsewhere_is_a_new_view():
    plan = plan_admission([memory(1, 0.0, 0.0, theta=0.0)], 0.0, 0.0, math.radians(120), stamp=1010.0)
    assert plan.record and plan.replace is None


def test_distance_alone_makes_a_new_view():
    plan = plan_admission([memory(1, 0.0, 0.0)], 1.5, 0.0, 0.0, stamp=1010.0)
    assert plan.record and plan.replace is None


def test_stale_viewpoint_is_refreshed_in_place():
    old = memory(1, 0.0, 0.0, stamp=1000.0)
    plan = plan_admission([old], 0.1, 0.0, 0.05, stamp=1000.0 + 31 * 60)
    assert plan.record and plan.replace == old and plan.evict is None


def test_at_capacity_evicts_the_older_of_the_closest_pair():
    spread = [memory(i, x=3.0 * i, stamp=500.0 + i) for i in range(MAX_MEMORIES - 2)]
    older_twin = memory(90, x=200.0, stamp=100.0)
    newer_twin = memory(91, x=200.4, stamp=200.0)
    plan = plan_admission([*spread, older_twin, newer_twin], -5.0, 0.0, 0.0, stamp=1000.0)
    assert plan.record and plan.evict == older_twin


# ================= store =================


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "A.pgm").write_bytes(b"map-A-content")
    (tmp_path / "maps" / "B.pgm").write_bytes(b"map-B-content")
    return tmp_path


def test_store_round_trips_across_instances(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    first = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.add(4.0, 2.0, 0.5, 1001.0, b"jpg-two")
    assert first is not None

    reloaded = MemoryStore(data_dir)
    reloaded.switch_map("A.yaml")
    snapshot = reloaded.snapshot()
    assert [m.id for m in snapshot.memories] == [1, 2]
    assert snapshot.memories[0] == first
    path = reloaded.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-one"


def test_remapping_under_the_same_name_wipes_stale_memories(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")

    (data_dir / "maps" / "A.pgm").write_bytes(b"remapped-content")
    reloaded = MemoryStore(data_dir)
    reloaded.switch_map("A.yaml")
    assert reloaded.snapshot().memories == ()
    assert list((data_dir / "spatial_memory" / "A").glob("*.jpg")) == []


def test_maps_have_isolated_memories(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.switch_map("B.yaml")
    assert store.snapshot().memories == ()
    store.switch_map("A.yaml")
    assert len(store.snapshot().memories) == 1


def test_eviction_removes_the_image_file(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    added = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert added is not None
    path = store.image_path(added.id)
    assert path is not None and path.exists()
    store.evict(added)
    assert not path.exists()
    assert store.snapshot().memories == ()


def test_replace_keeps_the_slot_and_updates_everything_else(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    added = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-old")
    assert added is not None
    store.replace(added, 1.1, 2.1, 0.6, 2000.0, b"jpg-new")
    (kept,) = store.snapshot().memories
    assert kept.id == added.id and kept.stamp == 2000.0 and kept.x == 1.1
    path = store.image_path(kept.id)
    assert path is not None and path.read_bytes() == b"jpg-new"


def test_without_a_map_nothing_is_recorded(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map(None)
    assert store.add(1.0, 2.0, 0.5, 1000.0, b"jpg") is None
    assert store.snapshot().memories == ()


def test_corrupt_index_resets_cleanly(data_dir):
    memory_dir = data_dir / "spatial_memory" / "A"
    memory_dir.mkdir(parents=True)
    (memory_dir / "index.json").write_text("{not json")
    (memory_dir / "7.jpg").write_bytes(b"orphan")
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    assert store.snapshot().memories == ()
    assert not (memory_dir / "7.jpg").exists()  # orphaned images have no poses; they are wiped with the index


# ================= recorder =================


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: state.now)
    return state


def make_recorder(data_dir, published: list | None = None):
    logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    node = SimpleNamespace(
        get_logger=lambda: logger,
        create_subscription=lambda *a, **k: None,
        create_timer=lambda *a, **k: None,
    )
    config = SimpleNamespace(
        image_topic="/img",
        current_nav_mode_topic="/nav/current_mode",
        current_map_topic="/nav/current_map",
        amcl_pose_topic="/amcl_pose",
    )
    store = MemoryStore(data_dir)
    recorder = MemoryRecorder(
        node,
        config,
        store=store,
        pose_tracker=SimpleNamespace(map_pose_xyt=lambda: (1.0, 2.0, 0.5)),
        warm_search=None,
        cache_state=lambda: "warm",
        positions_pub=SimpleNamespace(publish=(published if published is not None else []).append),
    )
    return recorder, store


def see_confident_world(recorder, clock):
    """Feed the recorder everything a recordable moment needs."""
    recorder._on_current_map(SimpleNamespace(data="A.yaml"))
    recorder._on_nav_mode(SimpleNamespace(data="navigation"))
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.02, 0.02, 0.05))))
    recorder._on_image(SimpleNamespace(data=b"live-frame"))
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -10.0})))


def _covariance(var_x: float, var_y: float, var_yaw: float) -> list[float]:
    covariance = [0.0] * 36
    covariance[0], covariance[7], covariance[35] = var_x, var_y, var_yaw
    return covariance


def test_confidence_must_hold_before_recording(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()  # starts the confidence clock
    assert store.snapshot().memories == ()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=b"live-frame"))
    recorder.tick()
    assert len(store.snapshot().memories) == 1


def test_a_covariance_spike_resets_the_clock(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 2.0
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.9, 0.9, 0.5))))
    recorder.tick()  # lost: the clock resets
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.02, 0.02, 0.05))))
    recorder.tick()  # confident again: the clock restarts here, 2s in
    clock.now += 2.9
    recorder._on_image(SimpleNamespace(data=b"live-frame"))
    recorder.tick()
    assert store.snapshot().memories == ()
    clock.now += 0.3
    recorder._on_image(SimpleNamespace(data=b"live-frame"))
    recorder.tick()
    assert len(store.snapshot().memories) == 1


def test_only_navigation_mode_records(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder._on_nav_mode(SimpleNamespace(data="mapping"))
    for _ in range(5):
        recorder.tick()
        clock.now += 2.0
        recorder._on_image(SimpleNamespace(data=b"live-frame"))
    assert store.snapshot().memories == ()


def test_looking_at_the_floor_blocks_capture(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -45.0})))
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=b"live-frame"))
    recorder.tick()
    assert store.snapshot().memories == ()


def test_a_stale_frame_blocks_capture(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1  # the frame from see_confident_world is now 3.1s old
    recorder.tick()
    assert store.snapshot().memories == ()


def test_positions_mirror_publishes_map_poses_and_cache_state(data_dir, clock):
    published: list = []
    recorder, store = make_recorder(data_dir, published)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=b"live-frame"))
    recorder.tick()
    payload = json.loads(published[-1].data)
    assert payload["map"] == "A.yaml" and payload["cache"] == "warm"
    (position,) = payload["positions"]
    assert position["id"] == 1 and position["x"] == 1.0 and position["y"] == 2.0 and position["theta"] == 0.5
    assert position["stamp"] > 0


# ================= memory search =================


def gemini_json(payload: dict) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(payload)}]}}]}


class FakeGemini:
    """A scriptable GeminiRest: records every call, answers by path."""

    def __init__(self, verdict: dict | None = None):
        self.creates: list[dict] = []
        self.generates: list[dict] = []
        self.deletes: list[str] = []
        self.create_error: GeminiHttpError | None = None
        self.cached_generate_error: GeminiHttpError | None = None
        self.verdict = verdict if verdict is not None else {"found": True, "frame": 1, "explanation": "matches"}
        self.rest = GeminiRest(post=self._post, delete=self._delete)

    def _post(self, path: str, body: dict) -> dict:
        if path == CACHED_CONTENTS_PATH:
            self.creates.append(body)
            if self.create_error is not None:
                raise self.create_error
            return {"name": f"cachedContents/c{len(self.creates)}"}
        self.generates.append(body)
        if "cachedContent" in body and self.cached_generate_error is not None:
            raise self.cached_generate_error
        return gemini_json(self.verdict)

    def _delete(self, path: str) -> dict:
        self.deletes.append(path)
        return {}


def make_search(data_dir, frames: int, verdict: dict | None = None) -> tuple[MemorySearch, FakeGemini, MemoryStore]:
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    for i in range(frames):
        store.add(float(3 * i), 0.0, 0.0, 1000.0 + i, f"jpg-{i + 1}".encode())
    fake = FakeGemini(verdict)
    logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    return MemorySearch(store, fake.rest, model="test-model", logger=logger), fake, store


def test_cache_is_created_once_and_reused(data_dir):
    search, fake, _ = make_search(data_dir, frames=6, verdict={"found": True, "frame": 2, "explanation": "the kitchen"})
    verdict = search.search("the kitchen")
    search.search("the kitchen again")
    assert len(fake.creates) == 1
    assert [body.get("cachedContent") for body in fake.generates] == ["cachedContents/c1", "cachedContents/c1"]
    assert verdict.found and verdict.cached and verdict.image == b"jpg-2"
    assert verdict.memory is not None and verdict.memory.x == 3.0
    assert "x=3.00m" in verdict_text(verdict) and "the kitchen" in verdict_text(verdict)
    assert "navigate_to_position" in verdict_text(verdict)


def test_new_memories_rebuild_the_cache_and_delete_the_old(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    search.search("first")
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    search.search("second")
    assert len(fake.creates) == 2
    assert fake.deletes == ["/v1beta/cachedContents/c1"]


def test_few_frames_answer_inline_without_caching(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)
    verdict = search.search("anything")
    assert fake.creates == []
    (body,) = fake.generates
    assert "systemInstruction" in body and "cachedContent" not in body
    assert sum("inlineData" in part for part in body["contents"][0]["parts"]) == 3
    assert verdict.image == b"jpg-1" and not verdict.cached


def test_unsupported_backend_disables_caching_permanently(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    fake.create_error = GeminiHttpError(404, "no such endpoint")
    verdict = search.search("first")
    search.search("second")
    assert len(fake.creates) == 1  # never attempted again
    assert all("cachedContent" not in body for body in fake.generates)
    assert verdict.image == b"jpg-1"


def test_failed_cache_creation_retries_only_after_the_memory_changes(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    fake.create_error = GeminiHttpError(400, "cache too small")
    search.search("first")
    search.search("second")
    assert len(fake.creates) == 1
    fake.create_error = None
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    search.search("third")
    assert len(fake.creates) == 2


def test_a_dead_cache_falls_back_inline_and_is_forgotten(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    search.search("first")
    fake.cached_generate_error = GeminiHttpError(403, "cache expired")
    verdict = search.search("second")
    assert verdict.image == b"jpg-1" and not verdict.cached
    assert "cachedContent" not in fake.generates[-1]  # answered inline
    fake.cached_generate_error = None
    search.search("third")
    assert len(fake.creates) == 2  # the dead handle was dropped, a fresh cache built


def test_no_match_is_a_clean_verdict_not_an_error(data_dir):
    search, fake, _ = make_search(data_dir, frames=6, verdict={"found": False, "frame": 0, "explanation": "no kitchen"})
    verdict = search.search("the kitchen")
    assert not verdict.found and verdict.image is None and verdict.error == ""
    assert "nothing in the remembered views matches" in verdict_text(verdict)


def test_unreadable_answer_becomes_an_error_verdict(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)
    fake.verdict = None  # type: ignore[assignment] — json.dumps(None) is valid JSON but not a verdict
    verdict = search.search("anything")
    assert verdict.error == "unreadable answer" and verdict.image is None
    assert "failed" in verdict_text(verdict)


def test_a_transport_crash_becomes_an_error_verdict_not_an_exception(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)

    def explode(path: str, body: dict) -> dict:
        raise RuntimeError("network down")

    search._rest = GeminiRest(post=explode, delete=lambda path: {})
    verdict = search.search("anything")
    assert not verdict.found and "network down" in verdict.error


def test_empty_memory_answers_without_the_network(data_dir):
    search, fake, _ = make_search(data_dir, frames=0)
    verdict = search.search("anything")
    assert fake.generates == [] and verdict.image is None and not verdict.found
    assert "no memories" in verdict_text(verdict)


def test_warm_builds_the_cache_in_the_background(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    store.last_change_monotonic = time.monotonic() - 60.0
    search.warm()
    deadline = time.time() + 2.0
    while search._cache is None and time.time() < deadline:
        time.sleep(0.01)
    assert search._cache is not None and len(fake.creates) == 1
    assert fake.generates == []  # warming never asks a question


def test_warm_waits_for_recording_to_settle(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    store.last_change_monotonic = time.monotonic()  # just changed
    search.warm()
    time.sleep(0.05)
    assert fake.creates == []


def test_search_mirrors_verdicts_to_the_ui(data_dir):
    search, fake, _ = make_search(data_dir, frames=6, verdict={"found": True, "frame": 2, "explanation": "kitchen"})
    reports: list[dict] = []
    search.on_result = reports.append
    search.search("the kitchen")
    (report,) = reports
    assert report["found"] and report["id"] == 2 and report["x"] == 3.0 and report["cached"]
    assert report["query"] == "the kitchen" and report["explanation"] == "kitchen"
    assert report["latency_sec"] >= 0 and report["stamp"] > 0 and report["seen_stamp"] == 1001.0

    fake.verdict = {"found": False, "frame": 0, "explanation": "no such view"}
    search.search("a unicorn")
    assert reports[-1] == {
        "query": "a unicorn",
        "found": False,
        "explanation": "no such view",
        "latency_sec": reports[-1]["latency_sec"],
        "cached": True,
        "stamp": reports[-1]["stamp"],
    }


def test_a_broken_ui_mirror_does_not_break_the_search(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)

    def explode(payload: dict) -> None:
        raise RuntimeError("publisher gone")

    search.on_result = explode
    verdict = search.search("anything")
    assert verdict.image == b"jpg-1"  # the search itself still succeeded


def test_cache_state_reflects_the_lifecycle(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    assert search.cache_state() == "cold"
    search.search("first")
    assert search.cache_state() == "warm"
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    assert search.cache_state() == "cold"

    small, _, _ = make_search(data_dir / "small", frames=0)
    assert small.cache_state() == "inline"

    unsupported, fake2, _ = make_search(data_dir / "unsup", frames=6)
    fake2.create_error = GeminiHttpError(404, "no endpoint")
    unsupported.search("first")
    assert unsupported.cache_state() == "unsupported"
