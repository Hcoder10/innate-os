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


def _scan_dirs(innate: Path, custom: Path, home: Path) -> list[Path]:
    """Ordered scan list: shipped first, then user (custom + home).

    The home directory is appended only when it exists, so it can be used as an
    in-place alternative to custom_* without ever being moved, and consumers
    (loaders, hot-reload watcher) never receive a non-existent path.
    """
    dirs = [innate, custom]
    if home.is_dir():
        dirs.append(home)
    return dirs


def get_agent_directories() -> list[Path]:
    """Directories to scan for agents: innate, custom_agents, then ~/agents."""
    return _scan_dirs(
        get_innate_agents_dir(), get_custom_agents_dir(), get_home_agents_dir()
    )


def get_skill_directories() -> list[Path]:
    """Directories to scan for skills: innate, custom_skills, then ~/skills."""
    return _scan_dirs(
        get_innate_skills_dir(), get_custom_skills_dir(), get_home_skills_dir()
    )


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
