"""Unit tests for the home-directory legacy migration in ``script_paths``.

Releases 0.4.x and 0.5.x scanned ``~/agents`` and ``~/skills`` (the literal home
directory) in addition to the repo. After the refactor those are migrated into
``workspace/custom_agents`` and ``workspace/custom_skills`` at brain startup by
``script_paths.migrate_legacy_home_directories()``.

``script_paths`` is pure-stdlib (no ROS imports), so we load it directly and run
on plain CI without building the workspace.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATHS = Path(__file__).resolve().parents[1] / "brain_client" / "script_paths.py"


def _load_script_paths():
    spec = importlib.util.spec_from_file_location("script_paths", _SCRIPT_PATHS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture()
def sp(tmp_path, monkeypatch):
    """``script_paths`` with HOME and INNATE_OS_ROOT pointed at temp dirs."""
    home = tmp_path / "home"
    repo = tmp_path / "innate-os"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("INNATE_OS_ROOT", str(repo))
    module = _load_script_paths()
    assert module.get_innate_os_root() == repo
    return module


def test_migrates_home_agents_and_skills(sp):
    home = Path.home()
    model = b"\x00\x01\x02trained-model-bytes"
    _write(home / "agents" / "my_agent.py", b"# home agent\n")
    _write(home / "skills" / "my_skill" / "model.h5", model)
    _write(home / "skills" / "my_skill" / "skill.py", b"# home skill\n")
    # __pycache__ must be ignored, never migrated into the workspace.
    _write(home / "agents" / "__pycache__" / "x.pyc", b"junk")

    sp.ensure_user_directories()
    sp.migrate_legacy_home_directories()

    assert (sp.get_custom_agents_dir() / "my_agent.py").read_bytes() == b"# home agent\n"
    # Trained model preserved byte-for-byte.
    assert (sp.get_custom_skills_dir() / "my_skill" / "model.h5").read_bytes() == model
    assert (sp.get_custom_skills_dir() / "my_skill" / "skill.py").read_bytes() == b"# home skill\n"
    assert not (sp.get_custom_agents_dir() / "__pycache__").exists()


def test_never_overwrites_existing_destination(sp):
    home = Path.home()
    _write(home / "agents" / "dup.py", b"home version\n")
    sp.ensure_user_directories()
    existing = sp.get_custom_agents_dir() / "dup.py"
    _write(existing, b"workspace version\n")

    sp.migrate_legacy_home_directories()

    # Destination is kept; the home copy is neither clobbered nor lost.
    assert existing.read_bytes() == b"workspace version\n"
    assert (home / "agents" / "dup.py").read_bytes() == b"home version\n"


def test_idempotent(sp):
    home = Path.home()
    _write(home / "skills" / "s" / "model.h5", b"abc")
    sp.ensure_user_directories()
    sp.migrate_legacy_home_directories()
    sp.migrate_legacy_home_directories()  # second pass must not raise or lose data
    assert (sp.get_custom_skills_dir() / "s" / "model.h5").read_bytes() == b"abc"
