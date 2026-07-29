# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hot-reload attribution: workspace changes reload everything, noise reloads nothing.

Under the import model a workspace package's modules import each other freely,
so no single skill owns an edit — every ``.py`` change under the workspace root
reloads the lot (empty lists, which the catalog reads as "reload everything").
Because that is one recursive watch on the root, a pack dropped in after boot
is picked up without a restart. Legacy out-of-workspace dirs keep per-item
attribution, which the agent side still consumes by name.

The one hard requirement: ``__pycache__`` must never count, or the ``.pyc``
files each reload writes would trigger the next one, forever.

Drives the watcher's resolution and debounce-flush steps directly. Part of the
fast pytest bucket in ci/run_integration_tests.sh.
"""

import logging
import threading
from types import SimpleNamespace

import pytest

from brain_client.skills.hot_reload_watcher import HotReloadWatcher

LOGGER = logging.getLogger("workspace_hot_reload_test")


@pytest.fixture
def env(tmp_path):
    """A watcher over a workspace root plus one legacy dir, recording reloads."""
    workspace = tmp_path / "workspace"
    (workspace / "custom_skills").mkdir(parents=True)
    legacy = tmp_path / "legacy_skills"
    legacy.mkdir()
    calls: list[tuple[list, list]] = []
    watcher = HotReloadWatcher(
        logger=LOGGER,
        skills_directories=[str(legacy)],
        agents_directories=[],
        on_reload=lambda skills, agents: calls.append((skills, agents)),
        debounce_seconds=0.05,
        recursive=True,
        workspace_roots=[str(workspace)],
    )
    return SimpleNamespace(watcher=watcher, workspace=workspace, legacy=legacy, calls=calls)


def _flush(env, *paths):
    """Feed changed paths through the watcher and run the debounced reload."""
    for path in paths:
        env.watcher._record_change(str(path))
    env.watcher._execute_reload()


def test_skill_edit_reloads_everything(env):
    _flush(env, env.workspace / "custom_skills" / "pick_socks.py")
    assert env.calls == [([], [])]


def test_helper_edit_reloads_everything(env):
    """A helper is a module like any other — importers must re-execute."""
    _flush(env, env.workspace / "custom_skills" / "geometry.py")
    assert env.calls == [([], [])]


def test_edit_in_a_subpackage_reloads_everything(env):
    _flush(env, env.workspace / "custom_skills" / "laundry" / "fold.py")
    assert env.calls == [([], [])]


def test_dropped_in_package_reloads_without_a_restart(env):
    """cp -r john_skills workspace/ — the root watch covers packages that did
    not exist when the watcher started."""
    _flush(env, env.workspace / "john_skills" / "chess.py")
    assert env.calls == [([], [])]


def test_symlinked_package_gets_its_own_watch_and_reloads(env, tmp_path):
    """`ln -s /opt/team/skills workspace/team_skills`: the pack imports like
    any workspace package, but its files live outside the workspace root, so
    the recursive root watch never sees them. Two things make edits reload
    anyway, both locked in here: the symlink is NOT treated as covered by the
    root watch (it gets its own scheduled watch), and an event reported at the
    pack's REAL path — watchdog resolves symlinks — still matches through the
    realpath'd skills-directory root."""
    real_pack = tmp_path / "elsewhere" / "team_skills"
    real_pack.mkdir(parents=True)
    link = env.workspace / "team_skills"
    link.symlink_to(real_pack)
    env.watcher.skills_directories = [*env.watcher.skills_directories, str(link)]

    # resolves outside the root -> needs (and gets) its own watch
    assert not env.watcher._inside_workspace_root(str(link))

    # watchdog reports the resolved path; must still attribute and reload
    _flush(env, real_pack / "wave.py")
    assert env.calls == [(["wave"], [])]


def test_pycache_is_never_a_change(env):
    """The reload writes these; counting them would loop forever."""
    _flush(env, env.workspace / "custom_skills" / "__pycache__" / "pick_socks.cpython-310.pyc")
    assert env.calls == []


def test_non_python_files_are_not_changes(env):
    _flush(env, env.workspace / "custom_skills" / "README.md", env.workspace / "custom_skills" / ".hidden" / "x.py")
    assert env.calls == []


def test_legacy_dir_keeps_per_item_attribution(env):
    """Out-of-workspace dirs still name what changed — the agent-side
    coordinator reloads by name."""
    _flush(env, env.legacy / "wave.py")
    assert env.calls == [(["wave"], [])]


def test_workspace_edit_subsumes_a_legacy_edit_in_the_same_window(env):
    _flush(env, env.legacy / "wave.py", env.workspace / "custom_skills" / "pick_socks.py")
    assert env.calls == [([], [])]  # reloading everything already covers wave


def test_edit_fires_through_the_debounce_timer(env):
    """End to end from a watchdog path event: debounce timer -> callback."""
    fired = threading.Event()
    env.watcher.on_reload = lambda skills, agents: (env.calls.append((skills, agents)), fired.set())
    env.watcher._on_path_changed(str(env.workspace / "custom_skills" / "pick_socks.py"))
    assert fired.wait(2.0), "debounced reload never fired"
    assert env.calls == [([], [])]
