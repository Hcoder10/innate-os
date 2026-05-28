"""
Centralized paths for agent and skill scripts.

Layout:
    $INNATE_OS_ROOT/workspace/innate_agents/   # shipped agents (tracked)
    $INNATE_OS_ROOT/workspace/custom_agents/   # user agents   (gitignored)
    $INNATE_OS_ROOT/workspace/innate_skills/   # shipped skills (tracked)
    $INNATE_OS_ROOT/workspace/custom_skills/   # user skills   (gitignored)
    ~/agents/                                  # user agents   (alternative, in place)
    ~/skills/                                  # user skills   (alternative, in place)

User content is supported in either workspace/custom_* or the home directory
(~/agents, ~/skills); both are scanned and home content is never moved.

Provenance is determined by which directory a script came from:
"shipped" if the path is under innate_*/, "user" otherwise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Source = Literal["shipped", "user"]


def get_innate_os_root() -> Path:
    return Path(
        os.environ.get(
            "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
        )
    )


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


def get_home_agents_dir() -> Path:
    """Optional user location ~/agents (an alternative to custom_agents)."""
    return Path.home() / "agents"


def get_home_skills_dir() -> Path:
    """Optional user location ~/skills (an alternative to custom_skills)."""
    return Path.home() / "skills"


def get_agent_directories() -> list[Path]:
    """Ordered list of directories to scan for agents.

    Shipped agents first, then user agents from workspace/custom_agents and,
    when it exists, the home directory (~/agents). Home content is scanned in
    place and never moved, so users may keep agents in either location.
    """
    dirs = [get_innate_agents_dir(), get_custom_agents_dir()]
    home = get_home_agents_dir()
    if home.is_dir():
        dirs.append(home)
    return dirs


def get_skill_directories() -> list[Path]:
    """Ordered list of directories to scan for skills.

    Shipped skills first, then user skills from workspace/custom_skills and,
    when it exists, the home directory (~/skills). Home content is scanned in
    place and never moved, so users may keep skills in either location.
    """
    dirs = [get_innate_skills_dir(), get_custom_skills_dir()]
    home = get_home_skills_dir()
    if home.is_dir():
        dirs.append(home)
    return dirs


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
