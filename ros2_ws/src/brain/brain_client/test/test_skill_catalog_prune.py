# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for stale-skill pruning: renaming or deleting a skill file must
remove its catalog entry instead of ghost-publishing it forever (previously
only a full reload_all cleaned it up)."""

import threading

from brain_client.skills.catalog import SkillRepository


class _Logger:
    def info(self, *a, **k):
        pass

    error = warning = warn = debug = info


def _repo(tmp_path, code=None, physical=None):
    repo = SkillRepository.__new__(SkillRepository)
    repo._logger = _Logger()
    repo._skills_lock = threading.Lock()
    repo._skills_directories = [str(tmp_path)]
    repo._code_skills = code or {}
    repo._physical_skills = physical or {}
    repo._in_training_skills = {}
    return repo


def _skill_entry(name):
    return (name, object())  # (display_name, instance) — instance is opaque here


def test_prune_removes_code_skill_whose_file_is_gone(tmp_path):
    (tmp_path / "keep.py").write_text("# still here")
    repo = _repo(tmp_path, code={"local/keep": _skill_entry("keep"), "local/gone": _skill_entry("gone")})

    removed = repo._prune_stale_skills()

    assert removed == ["local/gone"]
    assert list(repo._code_skills) == ["local/keep"]


def test_prune_is_prefix_aware(tmp_path):
    # a local foo.py must not keep a stale shipped innate-os/foo alive
    (tmp_path / "foo.py").write_text("# user's foo")
    repo = _repo(tmp_path, code={"innate-os/foo": _skill_entry("foo"), "local/foo": _skill_entry("foo")})

    removed = repo._prune_stale_skills()

    assert removed == ["innate-os/foo"]
    assert list(repo._code_skills) == ["local/foo"]


def test_prune_removes_physical_skill_whose_directory_is_gone(tmp_path):
    kept_dir = tmp_path / "kept"
    kept_dir.mkdir()
    (kept_dir / "metadata.json").write_text("{}")
    repo = _repo(
        tmp_path,
        physical={
            "local/kept": {"directory": str(kept_dir)},
            "local/gone": {"directory": str(tmp_path / "gone")},
        },
    )

    removed = repo._prune_stale_skills()

    assert removed == ["local/gone"]
    assert list(repo._physical_skills) == ["local/kept"]


def test_rename_prunes_old_id_and_reloads_new(tmp_path):
    # the drive_straight.py -> drive.py case: watcher reports only the new stem
    (tmp_path / "drive.py").write_text("# renamed skill")
    repo = _repo(tmp_path, code={"local/drive_straight": _skill_entry("drive_straight")})
    reloaded, published = [], []
    repo.reload_selective = reloaded.append
    repo.publish_skills_list = lambda: published.append(True)

    repo._on_skills_file_changed(["drive"], [])

    assert "local/drive_straight" not in repo._code_skills  # ghost purged
    assert reloaded == [["local/drive"]]  # new skill loaded


def test_pure_delete_publishes_without_full_reload(tmp_path):
    repo = _repo(tmp_path, code={"local/gone": _skill_entry("gone")})
    events = []
    repo.reload_selective = lambda ids: events.append(("selective", ids))
    repo.reload_all = lambda: events.append(("all",))
    repo.publish_skills_list = lambda: events.append(("publish",))

    repo._on_skills_file_changed(["gone"], [])

    assert repo._code_skills == {}
    assert events == [("publish",)]  # no reload_all storm for a delete


def test_unresolvable_change_still_falls_back_to_reload_all(tmp_path):
    repo = _repo(tmp_path)
    events = []
    repo.reload_all = lambda: events.append("all")

    repo._on_skills_file_changed(["mystery"], [])

    assert events == ["all"]
