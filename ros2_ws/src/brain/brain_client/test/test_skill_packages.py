# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Import-based skill discovery: skills register by existing.

The model: workspace packages are imported with
ordinary ``importlib`` machinery, and defining a ``Skill`` subclass registers
it (``Skill.__init_subclass__``) — no file scanning, no filename identity, no
guessing. A module that raises is broken; one that registers nothing is a
helper; each registered class is one skill, ``<namespace>/<snake(ClassName)>``.

ROS-free: exercises workspace_import and script_paths directly (no catalog,
no brain_messages). Part of the fast pytest bucket in
ci/run_integration_tests.sh.
"""

import logging
import sys
import textwrap

import pytest

from brain_client.common import script_paths
from brain_client.skills import workspace_import
from brain_client.skills.types import Skill

LOGGER = logging.getLogger("skill_packages_test")

SKILL_TEMPLATE = textwrap.dedent(
    """
    from brain_client.skills.types import Skill, SkillResult

    class {cls}(Skill):
        \"\"\"A test skill.\"\"\"

        def execute(self):
            return "ok", SkillResult.SUCCESS

        def cancel(self):
            pass
    """
)


@pytest.fixture(autouse=True)
def _pristine_import_state():
    """Discovery is real imports — restore sys.path and sys.modules so one
    test's tmp-dir modules can't satisfy another test's imports."""
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    yield
    sys.path[:] = saved_path
    for name in set(sys.modules) - saved_modules:
        del sys.modules[name]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    ws = tmp_path / "workspace"
    for name in ("innate_skills", "custom_skills"):
        (ws / name).mkdir(parents=True)
    return ws


def _discover(workspace):
    """One discovery pass: import everything, return (skills, errors)."""
    errors = workspace_import.import_workspace_packages(LOGGER)
    return workspace_import.registered_workspace_skills(LOGGER), errors


# --- package scan ---


def test_dropped_in_directory_is_a_package(workspace):
    (workspace / "john_skills").mkdir()
    (workspace / "john_skills" / "chess.py").write_text(SKILL_TEMPLATE.format(cls="Chess"))
    skills, errors = _discover(workspace)
    assert "john_skills/chess" in skills
    assert errors == {}


def test_machinery_dirs_are_not_packages(workspace):
    for name in ("innate_agents", "custom_agents", "inputs", "skill_lib", "skill_storage", "_drafts", ".git"):
        (workspace / name).mkdir()
    assert script_paths.get_workspace_package_dirs() == []


# --- registration is the discovery ---


def test_defining_a_skill_registers_it(workspace):
    (workspace / "custom_skills" / "pick_socks.py").write_text(SKILL_TEMPLATE.format(cls="PickSocks"))
    skills, errors = _discover(workspace)
    assert "local/pick_socks" in skills
    assert errors == {}
    _name, cls, src = skills["local/pick_socks"]
    assert cls.__module__ == "custom_skills.pick_socks"
    assert src == workspace / "custom_skills" / "pick_socks.py"


def test_two_skills_in_one_file_are_two_skills(workspace):
    (workspace / "custom_skills" / "laundry.py").write_text(
        SKILL_TEMPLATE.format(cls="FoldLaundry") + "\n" + SKILL_TEMPLATE.format(cls="SortLaundry")
    )
    skills, _ = _discover(workspace)
    assert {"local/fold_laundry", "local/sort_laundry"} <= set(skills)


def test_class_nested_skill_survives_the_liveness_check(workspace):
    """The staleness check must resolve the FULL qualname: matching only its
    first segment finds the enclosing class, fails the identity check, and
    silently prunes a live skill — the exact vanishing this model forbids."""
    (workspace / "custom_skills" / "boards.py").write_text(
        textwrap.dedent(
            """
            from brain_client.skills.types import Skill, SkillResult

            class Chess:
                class DetectMove(Skill):
                    \"\"\"A nested test skill.\"\"\"

                    def execute(self):
                        return "ok", SkillResult.SUCCESS

                    def cancel(self):
                        pass
            """
        )
    )
    skills, errors = _discover(workspace)
    assert errors == {}
    assert "local/detect_move" in skills


def test_function_local_skill_is_pruned_with_a_warning(workspace, caplog):
    """A Skill defined inside a function is unreachable from its module, so
    it cannot be loaded — but the module is live, so this is an authoring
    mistake and must be said out loud, not silently pruned."""
    (workspace / "custom_skills" / "factory.py").write_text(
        textwrap.dedent(
            """
            from brain_client.skills.types import Skill, SkillResult

            def make():
                class Hidden(Skill):
                    \"\"\"A function-local test skill.\"\"\"

                    def execute(self):
                        return "ok", SkillResult.SUCCESS

                    def cancel(self):
                        pass

            make()
            """
        )
    )
    with caplog.at_level(logging.WARNING):
        skills, errors = _discover(workspace)
    assert errors == {}
    assert not any("hidden" in skill_id for skill_id in skills)
    assert any("inside a function" in record.message for record in caplog.records)


def test_private_and_abstract_classes_are_helper_bases(workspace, caplog):
    """``_``-prefixed = deliberate helper base, skipped quietly. Abstract =
    usually a misspelled abstract method — skipped LOUDLY, or the skill
    vanishes from the roster undiagnosably (the old loader's warning must
    not regress under import-based discovery)."""
    (workspace / "custom_skills" / "bases.py").write_text(
        textwrap.dedent(
            """
            import abc
            from brain_client.skills.types import Skill, SkillResult

            class _PickBase(Skill):
                def execute(self):
                    return "ok", SkillResult.SUCCESS
                def cancel(self):
                    pass

            class StillAbstract(Skill, abc.ABC):
                @abc.abstractmethod
                def helper(self): ...
                def execute(self):
                    return "ok", SkillResult.SUCCESS
                def cancel(self):
                    pass
            """
        )
    )
    with caplog.at_level(logging.WARNING):
        skills, errors = _discover(workspace)
    assert errors == {}
    assert not any(i.endswith(("pick_base", "still_abstract")) for i in skills)
    warnings = [r.message for r in caplog.records]
    assert any("StillAbstract" in w and "helper" in w for w in warnings)
    assert not any("_PickBase" in w for w in warnings)


# --- helper vs broken, exactly ---


def test_module_registering_nothing_is_a_helper(workspace):
    (workspace / "custom_skills" / "geometry.py").write_text("ANSWER = 42\n")
    skills, errors = _discover(workspace)
    assert errors == {}
    assert not any(i.endswith("geometry") for i in skills)


def test_module_that_raises_is_broken_with_its_module_name(workspace):
    (workspace / "custom_skills" / "sock_utils.py").write_text("import does_not_exist_anywhere\n")
    _, errors = _discover(workspace)
    assert list(errors) == ["custom_skills.sock_utils"]
    assert "does_not_exist_anywhere" in errors["custom_skills.sock_utils"]
    assert workspace_import.module_skill_id("custom_skills.sock_utils") == "local/sock_utils"


# --- normal Python works ---


def test_relative_imports_inside_a_package(workspace):
    pack = workspace / "custom_skills" / "laundry"
    pack.mkdir()
    (pack / "__init__.py").write_text("")
    (pack / "geometry.py").write_text("ANSWER = 42\n")
    (pack / "fold.py").write_text(
        "from .geometry import ANSWER\nassert ANSWER == 42\n" + SKILL_TEMPLATE.format(cls="FoldLaundry")
    )
    skills, errors = _discover(workspace)
    assert errors == {}
    assert "local/fold_laundry" in skills


def test_packages_import_each_other_by_bare_name(workspace):
    (workspace / "john_skills").mkdir()
    (workspace / "john_skills" / "board_vision.py").write_text("SQUARES = 64\n")
    (workspace / "custom_skills" / "uses_pack.py").write_text(
        "from john_skills import board_vision\nassert board_vision.SQUARES == 64\n"
        + SKILL_TEMPLATE.format(cls="UsesPack")
    )
    skills, errors = _discover(workspace)
    assert errors == {}
    assert "local/uses_pack" in skills


def test_physical_skill_dirs_are_data_not_modules(workspace):
    greet = workspace / "custom_skills" / "greet"
    greet.mkdir()
    (greet / "metadata.json").write_text("{}")
    (greet / "notes.py").write_text("raise RuntimeError('never imported')\n")
    _, errors = _discover(workspace)
    assert errors == {}


# --- reload: evict then re-import picks up edits ---


def test_evict_and_reimport_picks_up_edits(workspace):
    target = workspace / "custom_skills" / "wave.py"
    target.write_text(SKILL_TEMPLATE.format(cls="Wave"))
    skills, _ = _discover(workspace)
    first = skills["local/wave"][1]

    target.write_text(SKILL_TEMPLATE.format(cls="Wave").replace("A test skill.", "Edited."))
    from brain_client.common.dynamic_loader import evict_modules_under

    evict_modules_under([str(workspace)])
    skills, _ = _discover(workspace)
    second = skills["local/wave"][1]
    assert second is not first
    assert "Edited." in (second.__doc__ or "")


def test_symlinked_package_discovers_and_hot_reloads(workspace, tmp_path):
    """`ln -s /opt/team/skills workspace/team_skills` is the supported way to
    load a skill pack that lives outside the repo (it replaced the 0.6.x
    extra_skill_dirs setting). Discovery must find it, and the catalog's
    eviction must drop its modules on reload even though the pack's real path
    is outside workspace/ — that is why _evict_workspace_modules adds every
    skills-directory that *resolves* outside the root as its own eviction
    root. Locks that in: eviction with the workspace root alone would silently
    keep the stale module."""
    real_pack = tmp_path / "elsewhere" / "team_skills"
    real_pack.mkdir(parents=True)
    (real_pack / "wave.py").write_text(SKILL_TEMPLATE.format(cls="Wave"))
    (workspace / "team_skills").symlink_to(real_pack)

    skills, errors = _discover(workspace)
    assert errors == {}
    first = skills["team_skills/wave"][1]

    (real_pack / "wave.py").write_text(SKILL_TEMPLATE.format(cls="Wave").replace("A test skill.", "Edited."))
    from pathlib import Path

    from brain_client.common.dynamic_loader import evict_modules_under

    # the catalog's exact eviction-root computation (_evict_workspace_modules)
    extra = [str(d) for d in script_paths.get_skill_directories() if not Path(d).resolve().is_relative_to(workspace)]
    assert str(workspace / "team_skills") in extra  # resolves outside -> own eviction root
    assert evict_modules_under([str(workspace), *extra])

    skills, _ = _discover(workspace)
    second = skills["team_skills/wave"][1]
    assert second is not first
    assert "Edited." in (second.__doc__ or "")


def test_stale_registrations_prune_after_evict(workspace):
    target = workspace / "custom_skills" / "wave.py"
    target.write_text(SKILL_TEMPLATE.format(cls="Wave"))
    _discover(workspace)
    target.unlink()
    from brain_client.common.dynamic_loader import evict_modules_under

    evict_modules_under([str(workspace)])
    skills, _ = _discover(workspace)
    assert "local/wave" not in skills
    assert ("custom_skills.wave", "Wave") not in Skill._registry


# --- ids ---


def test_namespaces_map_standard_dirs_and_packages(workspace):
    (workspace / "innate_skills" / "wave.py").write_text(SKILL_TEMPLATE.format(cls="Wave"))
    (workspace / "john_skills").mkdir()
    (workspace / "john_skills" / "wave.py").write_text(SKILL_TEMPLATE.format(cls="Wave"))
    skills, _ = _discover(workspace)
    assert {"innate-os/wave", "john_skills/wave"} <= set(skills)
