# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for per-skill persistent storage (self.storage): JSON-backed
dict access, lazy load, atomic writes, and the workspace path wiring."""

import json
import logging

import pytest

from brain_client.skills.types import Skill, SkillResult, SkillStorage


def test_roundtrip_and_persistence_across_instances(tmp_path):
    path = tmp_path / "my_skill.json"
    storage = SkillStorage(path)
    storage["dock_pose"] = {"x": 1.2, "y": 0.4}
    storage["runs"] = 3

    fresh = SkillStorage(path)  # a later process
    assert fresh["dock_pose"] == {"x": 1.2, "y": 0.4}
    assert fresh.get("runs") == 3
    assert fresh.get("missing", "fallback") == "fallback"
    assert "dock_pose" in fresh and "missing" not in fresh


def test_delete_persists_and_missing_key_raises(tmp_path):
    storage = SkillStorage(tmp_path / "s.json")
    storage["k"] = 1
    del storage["k"]
    assert "k" not in SkillStorage(tmp_path / "s.json")
    with pytest.raises(KeyError):
        storage["k"]


def test_writes_are_atomic_no_tmp_left_behind(tmp_path):
    storage = SkillStorage(tmp_path / "s.json")
    storage["k"] = {"nested": [1, 2, 3]}
    assert json.loads((tmp_path / "s.json").read_text()) == {"k": {"nested": [1, 2, 3]}}
    assert list(tmp_path.iterdir()) == [tmp_path / "s.json"]  # tmp file replaced, not left


def test_corrupt_file_degrades_to_empty(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    storage = SkillStorage(path)
    assert storage.get("anything") is None
    storage["fresh"] = True  # and it can recover by writing
    assert SkillStorage(path)["fresh"] is True


def test_skill_storage_property_lands_in_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))

    class Saver(Skill):
        @property
        def name(self):
            return "saver"

        def execute(self):
            self.storage["counter"] = self.storage.get("counter", 0) + 1
            return "ok", SkillResult.SUCCESS

    skill = Saver(logging.getLogger("test"))
    skill.execute()
    skill.execute()

    stored = json.loads((tmp_path / "workspace" / "skill_storage" / "saver.json").read_text())
    assert stored == {"counter": 2}
