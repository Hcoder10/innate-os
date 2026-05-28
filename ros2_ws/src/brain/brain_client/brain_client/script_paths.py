"""
Centralized paths for agent and skill scripts.

Layout:
    $INNATE_OS_ROOT/workspace/innate_agents/   # shipped agents (tracked)
    $INNATE_OS_ROOT/workspace/custom_agents/   # user agents   (gitignored)
    $INNATE_OS_ROOT/workspace/innate_skills/   # shipped skills (tracked)
    $INNATE_OS_ROOT/workspace/custom_skills/   # user skills   (gitignored)

Provenance is determined by which directory a script came from:
"shipped" if the path is under innate_*/, "user" otherwise.
"""

from __future__ import annotations

import os
import shutil
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


def get_agent_directories() -> list[Path]:
    """Ordered list of directories to scan for agents."""
    return [get_innate_agents_dir(), get_custom_agents_dir()]


def get_skill_directories() -> list[Path]:
    """Ordered list of directories to scan for skills."""
    return [get_innate_skills_dir(), get_custom_skills_dir()]


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


def migrate_legacy_home_directories(logger=None) -> None:
    """Move any leftover ~/agents and ~/skills content into the custom_* dirs.

    One-shot migration. Skips files that already exist at the destination.
    Safe to call on every startup.
    """
    pairs = [
        (Path.home() / "agents", get_custom_agents_dir()),
        (Path.home() / "skills", get_custom_skills_dir()),
    ]
    for src, dst in pairs:
        if not src.exists() or not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            if entry.name in ("__pycache__",):
                continue
            target = dst / entry.name
            if target.exists():
                if logger:
                    logger.warning(
                        f"Skipping migration of {entry} — {target} already exists."
                    )
                continue
            try:
                shutil.move(str(entry), str(target))
                if logger:
                    logger.info(f"Migrated {entry} -> {target}")
            except Exception as e:
                if logger:
                    logger.error(f"Failed to migrate {entry}: {e}")
