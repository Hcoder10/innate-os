"""Hot-reload path resolution: which skill/agent a changed file belongs to.

Pins the contract that editing a physical skill's ``metadata.json`` or assets
(which live in ``<skills_root>/<skill>/...``) reloads that skill, while editor
and build noise (``__pycache__``, dotfiles, atomic-write temp files) does not.

Pure path math — no watchdog observer, no rclpy. Part of the fast (no-ROS)
pytest bucket in ci/run_integration_tests.sh.
"""

from pathlib import Path

from brain_client.skills.hot_reload_watcher import (
    HotReloadWatcher,
    _flat_py_name_for,
    _InternalHandler,
    _skill_name_for,
)

SKILLS_ROOT = Path("/ws/innate_skills")
AGENTS_ROOT = Path("/ws/innate_agents")


# --- physical skills: the bug this fixes -----------------------------------
def test_physical_skill_metadata_reloads_its_skill():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "pick_socks" / "metadata.json") == "pick_socks"


def test_physical_skill_asset_reloads_its_skill():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "pick_socks" / "assets" / "demo.png") == "pick_socks"


def test_physical_skill_nested_python_reloads_its_skill():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "wave" / "policy.py") == "wave"


# --- top-level code skills (unchanged behavior) ----------------------------
def test_top_level_code_skill_uses_stem():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "scan_for_objects.py") == "scan_for_objects"


def test_top_level_non_python_is_not_a_skill():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "notes.md") is None


def test_dunder_and_private_top_level_files_skipped():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "__init__.py") is None
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "_helper.py") is None


# --- noise that must NOT trigger a reload ----------------------------------
def test_pycache_does_not_loop():
    # The .pyc written while reloading a skill must not re-trigger a reload.
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "__pycache__" / "scan.cpython-310.pyc") is None
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "pick_socks" / "__pycache__" / "policy.pyc") is None


def test_hidden_and_temp_files_skipped():
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "wave" / ".git" / "config") is None
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "wave" / ".DS_Store") is None
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "wave" / "metadata.json.tmp") is None
    assert _skill_name_for(SKILLS_ROOT, SKILLS_ROOT / "_private" / "metadata.json") is None


def test_file_outside_root_ignored():
    assert _skill_name_for(SKILLS_ROOT, Path("/somewhere/else/foo.py")) is None


# --- agents stay flat: only top-level .py, nested files ignored -------------
def test_agent_top_level_python():
    assert _flat_py_name_for(AGENTS_ROOT, AGENTS_ROOT / "basic_agent.py") == "basic_agent"


def test_agent_nested_asset_ignored():
    assert _flat_py_name_for(AGENTS_ROOT, AGENTS_ROOT / "assets" / "icon.png") is None


# --- end-to-end through the watcher's resolver / handler -------------------
def test_resolver_tags_skill_vs_agent():
    watcher = HotReloadWatcher(
        logger=None,
        skills_directories=[str(SKILLS_ROOT)],
        agents_directories=[str(AGENTS_ROOT)],
        on_reload=lambda skills, agents: None,
        recursive=True,
    )
    assert watcher._resolve(str(SKILLS_ROOT / "pick_socks" / "metadata.json")) == ("pick_socks", True)
    assert watcher._resolve(str(AGENTS_ROOT / "basic_agent.py")) == ("basic_agent", False)
    assert watcher._resolve("/nope/x.py") is None


def test_atomic_save_rename_is_forwarded():
    """Editors that save atomically arrive as a move onto the target, not a modify."""
    seen: list[str] = []
    handler = _InternalHandler(logger=None, on_path_changed=seen.append)

    class MovedEvent:
        is_directory = False
        src_path = str(SKILLS_ROOT / "pick_socks" / ".metadata.json.swp")
        dest_path = str(SKILLS_ROOT / "pick_socks" / "metadata.json")

    handler.on_moved(MovedEvent())
    assert seen == [str(SKILLS_ROOT / "pick_socks" / "metadata.json")]
