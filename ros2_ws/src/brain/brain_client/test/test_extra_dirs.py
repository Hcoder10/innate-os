"""Config-driven extra agent/skill scan dirs.

A user can point at agent/skill directories anywhere on the machine via the
``[paths]`` section of config/os.toml. The env loader exports them as
``INNATE_EXTRA_AGENT_DIRS`` / ``INNATE_EXTRA_SKILL_DIRS``; script_paths folds
them into the scan lists that both the loaders and the hot-reload watchers
consume — so the extra dirs load and hot-reload like workspace/.

Pure Python (no rclpy). Part of the fast (no-ROS) pytest bucket in
ci/run_integration_tests.sh.
"""

import os

import pytest
from maurice_bringup.env_loader import _join_dirs, _load_os_config

from brain_client.common import script_paths


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate INNATE_OS_ROOT/HOME and clear the extra-dir env so each test starts clean."""
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path / "innate-os"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("INNATE_EXTRA_AGENT_DIRS", raising=False)
    monkeypatch.delenv("INNATE_EXTRA_SKILL_DIRS", raising=False)
    return tmp_path


# --- script_paths folds the env dirs into the scan lists -------------------
def test_extra_skill_dir_is_scanned(isolated_env, monkeypatch):
    extra = isolated_env / "team-skills"
    extra.mkdir()
    monkeypatch.setenv("INNATE_EXTRA_SKILL_DIRS", str(extra))
    assert extra in script_paths.get_skill_directories()


def test_extra_agent_dir_is_scanned(isolated_env, monkeypatch):
    extra = isolated_env / "team-agents"
    extra.mkdir()
    monkeypatch.setenv("INNATE_EXTRA_AGENT_DIRS", str(extra))
    assert extra in script_paths.get_agent_directories()


def test_multiple_dirs_split_on_pathsep(isolated_env, monkeypatch):
    a, b = isolated_env / "a", isolated_env / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("INNATE_EXTRA_SKILL_DIRS", os.pathsep.join([str(a), str(b)]))
    dirs = script_paths.get_skill_directories()
    assert a in dirs and b in dirs


def test_home_is_expanded(isolated_env, monkeypatch):
    monkeypatch.setenv("INNATE_EXTRA_SKILL_DIRS", "~/elsewhere")
    assert script_paths.get_extra_skill_dirs() == [script_paths.Path.home() / "elsewhere"]


def test_blank_entries_dropped(isolated_env, monkeypatch):
    monkeypatch.setenv("INNATE_EXTRA_AGENT_DIRS", f"{os.pathsep}  {os.pathsep}")
    assert script_paths.get_extra_agent_dirs() == []


def test_extra_dir_outside_innate_is_user_provenance(isolated_env):
    # Anywhere not under innate_*/ is "user" (skill ids -> local/<name>).
    assert script_paths.classify_source(isolated_env / "team-skills" / "foo.py") == "user"


# --- env loader translates config/os.toml [paths] -> env -------------------
def test_load_os_config_sets_extra_dir_env(isolated_env, monkeypatch):
    monkeypatch.delenv("INNATE_EXTRA_AGENT_DIRS", raising=False)
    monkeypatch.delenv("INNATE_EXTRA_SKILL_DIRS", raising=False)
    toml = isolated_env / "os.toml"
    # String form parses identically with or without tomllib (robot is 3.10).
    toml.write_text(
        '[paths]\nagent_dirs = "/opt/a:/opt/b"\nskill_dirs = "/srv/skills"\n',
        encoding="utf-8",
    )
    _load_os_config(toml)
    assert os.environ["INNATE_EXTRA_AGENT_DIRS"] == "/opt/a:/opt/b"
    assert os.environ["INNATE_EXTRA_SKILL_DIRS"] == "/srv/skills"


# --- _join_dirs normalizes string / list / junk ----------------------------
def test_join_dirs_string():
    assert _join_dirs("/a:/b") == "/a:/b"


def test_join_dirs_list():
    # tomllib (3.11+) may parse a TOML array into a list.
    assert _join_dirs(["/a", "/b"]) == os.pathsep.join(["/a", "/b"])


def test_join_dirs_drops_blanks_and_junk():
    assert _join_dirs(f"/a{os.pathsep}{os.pathsep} ") == "/a"
    assert _join_dirs(None) == ""
    assert _join_dirs(42) == ""
