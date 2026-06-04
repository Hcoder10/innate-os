"""
Centralized paths for agent, skill, and input scripts.

Layout:
    $INNATE_OS_ROOT/workspace/innate_agents/   # shipped agents (tracked)
    $INNATE_OS_ROOT/workspace/custom_agents/   # user agents   (gitignored)
    $INNATE_OS_ROOT/workspace/innate_skills/   # shipped skills (tracked)
    $INNATE_OS_ROOT/workspace/custom_skills/   # user skills   (gitignored)
    $INNATE_OS_ROOT/workspace/inputs/          # input devices
    $INNATE_OS_ROOT/agents/                     # legacy user agents   (<= 0.5.x, in place)
    $INNATE_OS_ROOT/skills/                     # legacy user skills   (<= 0.5.x, in place)
    $INNATE_OS_ROOT/inputs/                     # legacy input devices (<= 0.5.x, in place)
    ~/agents/                                   # user agents   (alternative, in place)
    ~/skills/                                   # user skills   (alternative, in place)

Backwards compatibility: through release 0.5.x, agents/skills/inputs were loaded
from $INNATE_OS_ROOT/{agents,skills,inputs} and ~/{agents,skills}. Those locations
are still scanned (in place — never moved or created) so content deployed against
older releases keeps loading. See test/test_backwards_compat.py.

Provenance is determined by which directory a script came from:
"shipped" if the path is under innate_*/, "user" otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Source = Literal["shipped", "user"]


def get_innate_os_root() -> Path:
    return Path(os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")))


def _workspace() -> Path:
    return get_innate_os_root() / "workspace"


def get_innate_agents_dir() -> Path:
    return _workspace() / "innate_agents"


def get_custom_agents_dir() -> Path:
    return _workspace() / "custom_agents"


def get_innate_skills_dir() -> Path:
    return _workspace() / "innate_skills"


def get_custom_skills_dir() -> Path:
    return _workspace() / "custom_skills"


def get_innate_inputs_dir() -> Path:
    return _workspace() / "inputs"


def get_legacy_root_agents_dir() -> Path:
    """Legacy in-place location $INNATE_OS_ROOT/agents (used through 0.5.x)."""
    return get_innate_os_root() / "agents"


def get_legacy_root_skills_dir() -> Path:
    """Legacy in-place location $INNATE_OS_ROOT/skills (used through 0.5.x)."""
    return get_innate_os_root() / "skills"


def get_legacy_root_inputs_dir() -> Path:
    """Legacy in-place location $INNATE_OS_ROOT/inputs (used through 0.5.x)."""
    return get_innate_os_root() / "inputs"


def get_home_agents_dir() -> Path:
    """Optional user location ~/agents (an alternative to custom_agents)."""
    return Path.home() / "agents"


def get_home_skills_dir() -> Path:
    """Optional user location ~/skills (an alternative to custom_skills)."""
    return Path.home() / "skills"


def _scan_dirs(required: list[Path], optional: list[Path]) -> list[Path]:
    """Ordered, de-duplicated scan list.

    ``required`` directories are always included (loaders tolerate missing ones).
    ``optional`` directories — legacy $INNATE_OS_ROOT/* and home locations kept
    for backwards compatibility — are appended only when they exist, so they act
    as in-place alternatives that are never created or moved.
    """
    dirs = list(required) + [d for d in optional if d.is_dir()]
    seen: set[str] = set()
    deduped: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            deduped.append(d)
    return deduped


def get_agent_directories() -> list[Path]:
    """Agent scan dirs: workspace innate + custom, then legacy $INNATE_OS_ROOT/agents
    and ~/agents (both kept for backwards compatibility, scanned in place)."""
    return _scan_dirs(
        [get_innate_agents_dir(), get_custom_agents_dir()],
        [get_legacy_root_agents_dir(), get_home_agents_dir()],
    )


def get_skill_directories() -> list[Path]:
    """Skill scan dirs: workspace innate + custom, then legacy $INNATE_OS_ROOT/skills
    and ~/skills (both kept for backwards compatibility, scanned in place)."""
    return _scan_dirs(
        [get_innate_skills_dir(), get_custom_skills_dir()],
        [get_legacy_root_skills_dir(), get_home_skills_dir()],
    )


def get_input_directories() -> list[Path]:
    """Input-device scan dirs: workspace/inputs, then legacy $INNATE_OS_ROOT/inputs
    (kept for backwards compatibility, scanned in place)."""
    return _scan_dirs([get_innate_inputs_dir()], [get_legacy_root_inputs_dir()])


def classify_source(path: str | os.PathLike) -> Source:
    """Return "shipped" if path lives under an innate_* dir, else "user"."""
    resolved = Path(path).resolve()
    innate_roots = [get_innate_agents_dir().resolve(), get_innate_skills_dir().resolve()]
    for root in innate_roots:
        try:
            resolved.relative_to(root)
            return "shipped"
        except ValueError:
            continue
    return "user"


def ensure_user_directories() -> None:
    """Create custom_* directories if they don't exist yet."""
    get_custom_agents_dir().mkdir(parents=True, exist_ok=True)
    get_custom_skills_dir().mkdir(parents=True, exist_ok=True)
