# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the generated physical_skills refs: rendering, the
did-you-mean __getattr__, and how TrainedSkill refs plug into agents
(skill_ids) and skills (annotation -> PhysicalSkill descriptor)."""

import logging
import sys
import types as pytypes

import pytest

from brain_client.skills.physical_refs import class_name_for, render_refs, write_refs
from brain_client.skills.types import Skill, TrainedSkill

ENTRIES = [
    {"id": "local/pick-socks", "guidelines": "Picks up socks.", "type": "learned", "episode_count": 42},
    {"id": "innate-os/wave_hello", "guidelines": "", "type": "replay", "episode_count": 1},
    {"id": "local/2nd-try", "guidelines": "", "type": "learned", "episode_count": 0},  # not an identifier
    {"id": "innate-os/pick_socks", "guidelines": "", "type": "learned", "episode_count": 3},  # name collision
]


def _exec_generated(monkeypatch):
    """Render ENTRIES and import the result as a real module."""
    # the generated file does `from innate import TrainedSkill`; satisfy it
    # without the full innate package (which pulls state modules)
    fake_innate = pytypes.ModuleType("innate")
    fake_innate.TrainedSkill = TrainedSkill
    monkeypatch.setitem(sys.modules, "innate", fake_innate)
    module = pytypes.ModuleType("physical_skills")
    exec(compile(render_refs(ENTRIES), "physical_skills/__init__.py", "exec"), module.__dict__)
    return module


def test_class_name_derivation():
    assert class_name_for("local/pick-socks") == "PickSocks"
    assert class_name_for("innate-os/wave_hello") == "WaveHello"
    assert class_name_for("local/2nd-try") == ""  # leading digit — no identifier


def test_generated_module_execs_with_refs(monkeypatch):
    module = _exec_generated(monkeypatch)
    assert issubclass(module.PickSocks, TrainedSkill)
    assert module.PickSocks.skill_id == "local/pick-socks"
    assert module.WaveHello.skill_id == "innate-os/wave_hello"
    assert "42 episodes" in module.PickSocks.__doc__
    # skipped entries are visible as comments, not silently dropped
    source = render_refs(ENTRIES)
    assert "skipped local/2nd-try" in source
    assert "skipped innate-os/pick_socks" in source


def test_generated_getattr_is_a_did_you_mean(monkeypatch):
    module = _exec_generated(monkeypatch)
    with pytest.raises(AttributeError, match="Available: PickSocks, WaveHello"):
        _ = module.Deleted
    empty = pytypes.ModuleType("physical_skills")
    exec(compile(render_refs([]), "physical_skills/__init__.py", "exec"), empty.__dict__)
    with pytest.raises(AttributeError, match="no physical skills on this robot"):
        _ = empty.Anything


def test_write_refs_skips_identical_content(tmp_path):
    logger = logging.getLogger("test")
    content = render_refs(ENTRIES)
    write_refs(tmp_path, content, logger)
    target = tmp_path / "__init__.py"
    first_inode = target.stat().st_ino
    write_refs(tmp_path, content, logger)
    assert target.stat().st_ino == first_inode  # no rewrite -> no watcher churn
    write_refs(tmp_path, render_refs(ENTRIES[:1]), logger)
    assert target.stat().st_ino != first_inode  # os.replace swaps in a new file
    assert target.read_text() == render_refs(ENTRIES[:1])


def test_agent_skill_ids_accepts_trained_skill_refs():
    from brain_client.agents.types import Agent

    class PickSocks(TrainedSkill):
        skill_id = "local/pick-socks"

    class A(Agent):
        @property
        def id(self):
            return "a"

        @property
        def display_name(self):
            return "A"

        def get_skills(self):
            return [PickSocks, "innate-os/wave"]

        def get_prompt(self):
            return ""

    assert A().skill_ids() == ["local/pick-socks", "innate-os/wave"]


def test_trained_skill_annotation_materializes_physical_descriptor():
    from brain_client.skills.types import PhysicalSkill

    class PickSocks(TrainedSkill):
        skill_id = "local/pick-socks"

    class Tidy(Skill):
        pick: PickSocks

        def execute(self):
            return "done"

    assert isinstance(vars(Tidy)["pick"], PhysicalSkill)
    assert vars(Tidy)["pick"].skill_id == "local/pick-socks"
    assert PhysicalSkill(PickSocks).skill_id == "local/pick-socks"
