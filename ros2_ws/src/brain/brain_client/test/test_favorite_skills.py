# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""FavoriteSkillsStore: sanitize, persist, and survive corrupt files.

ROS-free (favorites.py imports only stdlib), part of the fast pytest bucket in
ci/run_integration_tests.sh.
"""

import json
import logging

from brain_client.skills.favorites import FavoriteSkillsStore

logger = logging.getLogger(__name__)


def make_store(tmp_path):
    return FavoriteSkillsStore(tmp_path / "prefs" / "favorite_skills.json", logger)


def test_starts_empty_without_a_file(tmp_path):
    assert make_store(tmp_path).ids == []


def test_replace_persists_across_instances(tmp_path):
    store = make_store(tmp_path)
    assert store.replace(["innate-os/wave", "local/my_trick"]) == ["innate-os/wave", "local/my_trick"]
    assert make_store(tmp_path).ids == ["innate-os/wave", "local/my_trick"]


def test_replace_sanitizes_junk_without_raising(tmp_path):
    store = make_store(tmp_path)
    stored = store.replace(["innate-os/wave", 42, None, "  ", "innate-os/wave", " local/x "])
    assert stored == ["innate-os/wave", "local/x"]


def test_non_list_payload_clears_to_empty(tmp_path):
    store = make_store(tmp_path)
    store.replace(["innate-os/wave"])
    assert store.replace({"nope": True}) == []
    assert make_store(tmp_path).ids == []


def test_corrupt_file_degrades_to_empty(tmp_path):
    store = make_store(tmp_path)
    store.replace(["innate-os/wave"])
    (tmp_path / "prefs" / "favorite_skills.json").write_text("{not json")
    assert make_store(tmp_path).ids == []


def test_unknown_ids_are_kept(tmp_path):
    # Validation against the live roster is the consumer's job: a favorite must
    # survive its skill being mid-reload or temporarily unloaded.
    store = make_store(tmp_path)
    assert store.replace(["innate-os/not_loaded_right_now"]) == ["innate-os/not_loaded_right_now"]


def test_accepts_legacy_bare_array_file(tmp_path):
    path = tmp_path / "prefs" / "favorite_skills.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(["innate-os/wave"]))
    assert make_store(tmp_path).ids == ["innate-os/wave"]
