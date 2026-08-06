# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pins the rendered recording-folder shim content.

The shims used to be committed, so template drift showed up as a git diff at
review time; now they are runtime-only (gitignored) and this is the only pin.
Rendering is pure — no ROS, no filesystem.
"""

from brain_client.skills.physical_refs import (
    _SHIM_MARKER,
    class_name_for,
    render_dir_shims,
)

WAVE = {"id": "innate-os/wave", "dir": "/ws/innate_skills/wave"}


def test_plain_shim_exact_content():
    content = render_dir_shims([WAVE])["/ws/innate_skills/wave/__init__.py"]
    assert content == (
        "# AUTO-GENERATED physical-skill ref by the skill catalog — do not edit; edits are overwritten.\n"
        '"""Typed ref to \'innate-os/wave\', the recording in this folder.\n'
        "\n"
        "Same class either way:\n"
        "\n"
        "    from physical_skills import Wave\n"
        '"""\n'
        "\n"
        "from physical_skills import Wave\n"
        "\n"
        '__all__ = ["Wave"]\n'
    )


def test_shim_starts_with_marker():
    # write_dir_shims only ever overwrites/deletes files starting with the
    # marker — a template that drops it would strand every deployed shim as
    # "hand-written" and refuse all future updates.
    content = render_dir_shims([WAVE])["/ws/innate_skills/wave/__init__.py"]
    assert content.startswith(_SHIM_MARKER)


def test_collision_loser_shim_aliases_qualified_name():
    entries = [
        {"id": "local/wave", "dir": "/ws/custom_skills/wave"},
        {"id": "innate-os/wave", "dir": "/ws/innate_skills/wave"},
    ]
    shims = render_dir_shims(entries)
    # local/ claims the plain name; its shim is the plain form
    assert "from physical_skills import Wave\n" in shims["/ws/custom_skills/wave/__init__.py"]
    # the loser's folder keeps exporting its plain name under an alias
    loser = shims["/ws/innate_skills/wave/__init__.py"]
    assert "from physical_skills import WaveInnateOs as Wave\n" in loser
    assert '__all__ = ["Wave"]\n' in loser


def test_unnameable_entry_marks_shim_for_removal():
    entry = {"id": "innate-os/123", "dir": "/ws/innate_skills/123"}
    assert class_name_for(entry["id"]) == ""
    assert render_dir_shims([entry]) == {"/ws/innate_skills/123/__init__.py": None}


def test_entry_without_dir_gets_no_shim():
    assert render_dir_shims([{"id": "innate-os/wave"}]) == {}


def test_rendered_shims_are_valid_python():
    entries = [
        WAVE,
        {"id": "local/wave", "dir": "/ws/custom_skills/wave"},
        {"id": "local/pick-socks", "dir": "/ws/custom_skills/pick_socks"},
    ]
    for path, content in render_dir_shims(entries).items():
        if content is not None:
            compile(content, path, "exec")
