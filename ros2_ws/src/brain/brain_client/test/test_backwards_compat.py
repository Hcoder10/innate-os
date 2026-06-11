"""Backwards-compatibility guards for agent / skill / input discovery.

These pin the historical contract that the concept-folder refactor broke, so it
cannot silently break again:

1. **Locations** — through release 0.5.x, agents/skills/inputs loaded from
   ``$INNATE_OS_ROOT/{agents,skills,inputs}`` and ``~/{agents,skills}``. The
   refactor moved shipped/user content under ``workspace/`` and dropped the
   legacy roots. They must still be scanned (in place) so content deployed
   against an older release keeps loading.
2. **Import paths** — pre-refactor user files import their base class from
   ``brain_client.{skill,agent,input}_types`` and ``brain_client.logging_config``.
   Those module paths must still resolve to the *same* classes via the shims.

The end-to-end tests combine both: a file written to a legacy location AND
importing via a legacy module path (exactly what an old user file looks like)
must still be discovered by the loader.

Part of the fast (no-ROS) pytest bucket in ci/run_integration_tests.sh. Pure
Python + the ROS-free loaders; no rclpy.init / DDS needed.
"""

import logging
import textwrap
from pathlib import Path

import pytest

from brain_client.common import script_paths

LOGGER = logging.getLogger("backwards_compat_test")

# Old user file = old import path (the compat shim) + old on-disk location.
AGENT_TEMPLATE = textwrap.dedent("""
    from {import_path} import Agent

    class CompatAgent(Agent):
        @property
        def id(self):
            return "{name}"

        @property
        def display_name(self):
            return "Compat"

        def get_skills(self):
            return []

        def get_prompt(self):
            return "compat"
""")

SKILL_TEMPLATE = textwrap.dedent("""
    from {import_path} import Skill, SkillResult

    class CompatSkill(Skill):
        @property
        def name(self):
            return "{name}"

        def execute(self, *args, **kwargs):
            return "ok", SkillResult.SUCCESS

        def cancel(self):
            pass
""")


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """Point INNATE_OS_ROOT and HOME at isolated temp dirs (read per-call)."""
    root = tmp_path / "innate-os"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("INNATE_OS_ROOT", str(root))
    monkeypatch.setenv("HOME", str(home))  # Path.home() follows $HOME on POSIX
    return root, home


def _write(directory: Path, filename: str, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(content)


# --------------------------------------------------------------------------
# 1. Directory contract: legacy + home locations are scanned when present.
# --------------------------------------------------------------------------
def test_agent_dirs_include_legacy_and_home(fake_root):
    root, home = fake_root
    (root / "agents").mkdir()
    (home / "agents").mkdir()
    dirs = [str(p) for p in script_paths.get_agent_directories()]
    assert str(root / "agents") in dirs  # legacy $INNATE_OS_ROOT/agents
    assert str(home / "agents") in dirs  # ~/agents
    assert str(root / "workspace" / "innate_agents") in dirs
    assert str(root / "workspace" / "custom_agents") in dirs


def test_skill_dirs_include_legacy_and_home(fake_root):
    root, home = fake_root
    (root / "skills").mkdir()
    (home / "skills").mkdir()
    dirs = [str(p) for p in script_paths.get_skill_directories()]
    assert str(root / "skills") in dirs  # legacy $INNATE_OS_ROOT/skills
    assert str(home / "skills") in dirs  # ~/skills
    assert str(root / "workspace" / "innate_skills") in dirs
    assert str(root / "workspace" / "custom_skills") in dirs


def test_input_dirs_include_legacy(fake_root):
    root, _ = fake_root
    (root / "inputs").mkdir()
    dirs = [str(p) for p in script_paths.get_input_directories()]
    assert str(root / "inputs") in dirs  # legacy $INNATE_OS_ROOT/inputs
    assert str(root / "workspace" / "inputs") in dirs


def test_optional_dirs_absent_when_missing(fake_root):
    """Legacy/home dirs are never fabricated — only scanned if they exist."""
    root, home = fake_root
    dirs = [str(p) for p in script_paths.get_agent_directories()]
    assert str(root / "agents") not in dirs
    assert str(home / "agents") not in dirs


# --------------------------------------------------------------------------
# 2. End-to-end discovery from every historical location (old import + old path).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("location", ["root", "home"])
def test_agent_discovered_from_legacy_location(fake_root, location):
    from brain_client.agents.loader import AgentLoader

    root, home = fake_root
    target = (root / "agents") if location == "root" else (home / "agents")
    name = f"compat_{location}_agent"
    _write(target, "compat_agent.py", AGENT_TEMPLATE.format(import_path="brain_client.agent_types", name=name))

    discovered = AgentLoader(LOGGER).load_from_directories([str(p) for p in script_paths.get_agent_directories()])
    assert name in discovered


@pytest.mark.parametrize("location", ["root", "home"])
def test_skill_discovered_from_legacy_location(fake_root, location):
    from brain_client.skills.loader import SkillLoader

    root, home = fake_root
    target = (root / "skills") if location == "root" else (home / "skills")
    name = f"compat_{location}_skill"
    _write(target, "compat_skill.py", SKILL_TEMPLATE.format(import_path="brain_client.skill_types", name=name))

    discovered = SkillLoader(LOGGER).load_from_directories([str(p) for p in script_paths.get_skill_directories()])
    assert name in discovered


# --------------------------------------------------------------------------
# 3. Import-path shims resolve to the SAME classes as the new module paths.
# --------------------------------------------------------------------------
def test_skill_types_shim():
    import brain_client.skill_types as shim
    from brain_client.skills.types import Skill, SkillResult

    assert shim.Skill is Skill
    assert shim.SkillResult is SkillResult


def test_agent_types_shim():
    import brain_client.agent_types as shim
    from brain_client.agents.types import Agent

    assert shim.Agent is Agent


def test_input_types_shim():
    import brain_client.input_types as shim
    from brain_client.inputs.types import InputDevice

    assert shim.InputDevice is InputDevice


def test_logging_config_shim():
    import brain_client.logging_config as shim
    from brain_client.common.logging import UniversalLogger

    assert shim.UniversalLogger is UniversalLogger
