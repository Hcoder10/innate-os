"""Runtime loadability test for migrated agents.

The file-movement tests prove a migrated agent lands at
``workspace/custom_agents/``. This goes one step further: it runs the *real*
``AgentLoader`` (which is ROS-free) against the migrated directory and asserts
the agent is actually discovered AND validated — i.e. usable after an upgrade,
not merely present on disk.

(The skill loader cannot be exercised here because ``skill_types`` imports
``rclpy``/``brain_messages``; that path is covered by the ROS integration lane.)
"""

import logging
import sys
import textwrap
from pathlib import Path

import pytest

_PKG_PARENT = Path(__file__).resolve().parents[1]
if str(_PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(_PKG_PARENT))

from brain_client import script_paths  # noqa: E402
from brain_client.agent_loader import AgentLoader  # noqa: E402
from brain_client.agent_types import Agent  # noqa: E402

# A minimal but fully valid agent (properties + required methods, no-arg ctor).
_VALID_AGENT = textwrap.dedent(
    """
    from brain_client.agent_types import Agent


    class MyMigratedAgent(Agent):
        @property
        def id(self):
            return "my_migrated_agent"

        @property
        def display_name(self):
            return "My Migrated Agent"

        def get_skills(self):
            return ["wave"]

        def get_prompt(self):
            return "Be helpful."
    """
).lstrip()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "innate-os"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("INNATE_OS_ROOT", str(repo))
    return home, repo


def test_loader_discovers_agent_in_custom_agents(env):
    """The location the shell migration writes to is scanned by the real loader."""
    custom = script_paths.get_custom_agents_dir()
    custom.mkdir(parents=True)
    (custom / "my_migrated_agent.py").write_text(_VALID_AGENT)

    loader = AgentLoader(logging.getLogger("test"))
    discovered = loader.load_agents_from_directories([str(custom)])

    assert "my_migrated_agent" in discovered
    cls, src = discovered["my_migrated_agent"]
    assert issubclass(cls, Agent)
    assert Path(src).name == "my_migrated_agent.py"


def test_home_migrated_agent_is_loadable(env):
    """End-to-end: ~/agents -> migration -> discovered by the real loader."""
    home, _ = env
    (home / "agents").mkdir(parents=True)
    (home / "agents" / "my_migrated_agent.py").write_text(_VALID_AGENT)

    script_paths.ensure_user_directories()
    script_paths.migrate_legacy_home_directories()

    loader = AgentLoader(logging.getLogger("test"))
    discovered = loader.load_agents_from_directories([str(script_paths.get_custom_agents_dir())])

    assert "my_migrated_agent" in discovered, "migrated home agent was not discovered by the real AgentLoader"
