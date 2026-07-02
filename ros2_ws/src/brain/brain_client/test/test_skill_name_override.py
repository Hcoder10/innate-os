# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for display-name dedup in the published skills list: a user
(local/) skill overrides a shipped (innate-os/) one of the same name — matching
the bare-name precedence in SkillInvoker._resolve — while same-source
duplicates keep the first and skip the rest."""

from brain_messages.msg import SkillInfo

from brain_client.skills.catalog import SkillRepository


class _Logger:
    def info(self, *a, **k):
        pass

    error = warning = warn = debug = info


def _repo():
    repo = SkillRepository.__new__(SkillRepository)  # dedup only needs the logger
    repo._logger = _Logger()
    return repo


def _info(skill_id, name):
    msg = SkillInfo()
    msg.id = skill_id
    msg.name = name
    return msg


def _dedupe(entries):
    result = _repo()._dedupe_display_names([_info(i, n) for i, n in entries])
    return [(s.id, s.name) for s in result]


def test_user_skill_overrides_shipped_of_same_name():
    # shipped loads first (scan order), user later -- user must win
    assert _dedupe([("innate-os/wave", "wave"), ("local/wave", "wave")]) == [("local/wave", "wave")]


def test_override_holds_regardless_of_order():
    assert _dedupe([("local/wave", "wave"), ("innate-os/wave", "wave")]) == [("local/wave", "wave")]


def test_same_source_duplicates_keep_first():
    assert _dedupe([("local/wave", "wave"), ("local/wave2", "wave")]) == [("local/wave", "wave")]


def test_distinct_names_pass_through_in_order():
    entries = [("innate-os/wave", "wave"), ("local/hello", "Hello"), ("innate-os/nav", "nav")]
    assert _dedupe(entries) == entries


def test_override_keeps_list_position():
    # the overriding user skill takes the shipped skill's slot, not the end
    assert _dedupe([("innate-os/wave", "wave"), ("innate-os/nav", "nav"), ("local/wave", "wave")]) == [
        ("local/wave", "wave"),
        ("innate-os/nav", "nav"),
    ]
