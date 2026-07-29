# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Broken skills stay visible: catalog lifecycle from load to roster.

A skill file that fails to execute (typo'd import, syntax error) must not
silently vanish — the catalog keeps a broken entry with the error text,
publishes it on /brain/available_skills (SkillInfo.load_error), and clears it
when the file is fixed. A ``.py`` in a skills dir that defines no skill is a
helper: editing it reloads everything so importers re-execute. A helper that
fails to import is rostered broken under its own module id — exact knowledge,
not filename heuristics (workspace packages are imported, not exec'd).

Uses a stub node (logger + publisher recorder); no rclpy.init / DDS. Needs the
built workspace for brain_messages, so this runs in the CI image's fast pytest
bucket (ci/run_integration_tests.sh), not on a bare macOS checkout.
"""

import json
import sys
import textwrap
from types import SimpleNamespace

import pytest

from brain_client.skills.catalog import SkillRepository


@pytest.fixture(autouse=True)
def _pristine_import_state():
    """Loading skills mutates global import state (sys.path entries, helper
    modules cached from per-test tmp dirs). Restore both so a later test
    importing the same helper name gets its own file, not this test's."""
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    yield
    sys.path[:] = saved_path
    for name in set(sys.modules) - saved_modules:
        del sys.modules[name]


GOOD_SKILL = textwrap.dedent(
    """
    from brain_client.skills.types import Skill, SkillResult

    class {cls}(Skill):
        \"\"\"A test skill.\"\"\"

        @property
        def name(self):
            return "{name}"

        def execute(self):
            return "ok", SkillResult.SUCCESS

        def cancel(self):
            pass
    """
)


class _StubLogger:
    def debug(self, msg):
        pass

    info = warn = warning = error = fatal = debug


class _StubPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, msg):
        self.messages.append(msg)


class _StubNode:
    def __init__(self):
        self.publisher = _StubPublisher()

    def get_logger(self):
        return _StubLogger()

    def create_publisher(self, *_args, **_kwargs):
        return self.publisher


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    monkeypatch.setenv("INNATE_SKILL_CACHE", str(tmp_path / "skill_cache.json"))
    innate_skills = tmp_path / "workspace" / "innate_skills"
    innate_skills.mkdir(parents=True)
    custom_skills = tmp_path / "workspace" / "custom_skills"
    custom_skills.mkdir(parents=True)
    (custom_skills / "good_skill.py").write_text(GOOD_SKILL.format(cls="GoodSkill", name="good_skill"))
    # A realistic broken skill: the failing import sits above a Skill class.
    (custom_skills / "broken_skill.py").write_text(
        "import does_not_exist_anywhere\n" + GOOD_SKILL.format(cls="BrokenSkill", name="broken_skill")
    )
    node = _StubNode()
    return SimpleNamespace(
        node=node,
        repo=SkillRepository(node),
        custom=custom_skills,
        published=node.publisher.messages,
    )


def _roster_entry(env, skill_id):
    assert env.published, "nothing published"
    matches = [s for s in env.published[-1].skills if s.id == skill_id]
    return matches[0] if matches else None


def test_broken_skill_is_published_with_its_error(env):
    env.repo.publish_skills_list()

    good = _roster_entry(env, "local/good_skill")
    broken = _roster_entry(env, "local/broken_skill")
    assert good is not None and good.load_error == ""
    assert broken is not None
    assert broken.type == "broken"
    assert "does_not_exist_anywhere" in broken.load_error
    assert env.repo.get_load_error("broken_skill") == broken.load_error


def test_fixing_the_file_clears_the_broken_entry(env):
    (env.custom / "broken_skill.py").write_text(GOOD_SKILL.format(cls="BrokenSkill", name="broken_skill"))

    reloaded = env.repo.reload_selective(["local/broken_skill"])

    assert "local/broken_skill" in reloaded
    assert env.repo.get_load_error("broken_skill") is None
    assert _roster_entry(env, "local/broken_skill").load_error == ""


def test_breaking_a_working_skill_swaps_it_to_broken(env):
    (env.custom / "good_skill.py").write_text("def nope(:\n")

    env.repo.reload_selective(["local/good_skill"])

    assert env.repo.get_code_skill("local/good_skill") is None
    assert "SyntaxError" in (env.repo.get_load_error("good_skill") or "")
    assert "SyntaxError" in _roster_entry(env, "local/good_skill").load_error


def test_deleting_a_broken_file_prunes_the_entry(env):
    (env.custom / "broken_skill.py").unlink()

    env.repo.reload_all()

    assert env.repo.get_load_error("broken_skill") is None
    assert _roster_entry(env, "local/broken_skill") is None


def test_helper_edit_falls_back_to_full_reload(env):
    """A .py that defines no skill is a helper — reload_selective on its stem
    reloads everything (importers must re-execute) instead of no-opping."""
    (env.custom / "sock_utils.py").write_text("VALUE = 1\n")

    reloaded = env.repo.reload_selective(["local/sock_utils"])

    assert "local/good_skill" in reloaded  # full-reload result, not a no-op
    assert env.repo.get_load_error("sock_utils") is None


def test_broken_subpackage_module_still_answers_by_leaf_name(env):
    """custom_skills/boards/chess.py rosters broken as local/boards.chess —
    an id no bare_id_candidates() spelling ever produces, while the working
    class's id was local/chess. Calls to that id must still surface the load
    error, not a generic 'unknown skill'."""
    folder = env.custom / "boards"
    folder.mkdir()
    (folder / "chess.py").write_text("import does_not_exist_anywhere\n" + GOOD_SKILL.format(cls="Chess", name="chess"))

    env.repo.reload_all()

    assert _roster_entry(env, "local/boards.chess") is not None  # UI shows it
    # ...and the ids the agent/invoker actually dial reach the same error
    assert "does_not_exist_anywhere" in (env.repo.get_load_error("chess") or "")
    assert "does_not_exist_anywhere" in (env.repo.get_load_error("local/chess") or "")


def test_broken_helper_is_rostered_under_its_module_id(env):
    """A helper that fails to import is broken code the author should see —
    it rosters under its own module id with the real error."""
    (env.custom / "sock_utils.py").write_text("import does_not_exist_anywhere\nVALUE = 1\n")

    env.repo.reload_all()

    assert "does_not_exist_anywhere" in (env.repo.get_load_error("sock_utils") or "")
    assert _roster_entry(env, "local/sock_utils") is not None


def test_smashed_skill_stays_rostered_even_without_the_word_skill(env):
    """Breaking a known skill into unrecognizable garbage must not vanish it —
    the failed module itself is the broken entry."""
    (env.custom / "good_skill.py").write_text("def nope(:\n")

    env.repo.reload_all()

    assert "SyntaxError" in (env.repo.get_load_error("good_skill") or "")


def test_workspace_package_skill_gets_a_namespaced_id(env, tmp_path):
    """A dropped-in package under workspace/ namespaces its skills by dir name,
    and bare names resolve to it."""
    pack = tmp_path / "workspace" / "john_skills"
    pack.mkdir()
    (pack / "chess.py").write_text(GOOD_SKILL.format(cls="Chess", name="chess"))

    env.repo.reload_all()

    assert env.repo.get_code_skill("john_skills/chess") is not None
    assert _roster_entry(env, "john_skills/chess") is not None
    assert "john_skills/chess" in env.repo.bare_id_candidates("chess")


def test_skill_in_a_package_folder_with_relative_import(env):
    """A skill that outgrew one file is just a package: normal Python, the
    helper imported relatively, the id from the class name."""
    folder = env.custom / "socks"
    folder.mkdir()
    (folder / "__init__.py").write_text("")
    (folder / "geometry.py").write_text("ANSWER = 42\n")
    (folder / "pick.py").write_text(
        "from .geometry import ANSWER\nassert ANSWER == 42\n" + GOOD_SKILL.format(cls="PickSocks", name="pick_socks")
    )

    env.repo.reload_all()

    assert env.repo.get_code_skill("local/pick_socks") is not None
    assert _roster_entry(env, "local/pick_socks") is not None


def test_package_skill_with_shipped_display_name_is_published_qualified(env, tmp_path):
    """A package skill whose display name collides with a shipped one must not
    be dropped from the roster — it publishes under 'name (<package>)'."""
    (tmp_path / "workspace" / "innate_skills" / "wave.py").write_text(GOOD_SKILL.format(cls="Wave", name="wave"))
    pack = tmp_path / "workspace" / "john_skills"
    pack.mkdir()
    (pack / "wave.py").write_text(GOOD_SKILL.format(cls="Wave", name="wave"))

    env.repo.reload_all()

    published = {s.id: s.name for s in env.published[-1].skills}
    assert "innate-os/wave" in published and "john_skills/wave" in published
    assert {published["innate-os/wave"], published["john_skills/wave"]} == {"wave", "wave (john_skills)"}


def test_cold_boot_keeps_a_mangled_known_skill_visible(env, tmp_path):
    """A skill file mangled while the node was down must boot as a broken
    entry — the module fails to import, no history needed."""
    (env.custom / "good_skill.py").write_text("def nope(:\n")

    rebooted = SkillRepository(env.node)  # fresh instance = cold boot

    assert "SyntaxError" in (rebooted.get_load_error("good_skill") or "")


def test_invalid_physical_metadata_is_published_as_broken(env):
    bad_dir = env.custom / "bad_physical"
    bad_dir.mkdir()
    (bad_dir / "metadata.json").write_text("{not json")

    env.repo.reload_all()

    entry = _roster_entry(env, "local/bad_physical")
    assert entry is not None
    assert "invalid JSON" in entry.load_error


def test_skill_cache_carries_load_error(env, tmp_path):
    env.repo.publish_skills_list()
    payload = json.loads((tmp_path / "skill_cache.json").read_text())
    by_id = {s["id"]: s for s in payload["skills"]}
    assert by_id["local/good_skill"]["load_error"] == ""
    assert "does_not_exist_anywhere" in by_id["local/broken_skill"]["load_error"]
