"""Shared helpers for locating skill directories.

Skills are read from the configured primary directory (workspace/custom_skills)
and, when it exists, the home directory (~/skills). This supports users who
keep — or previously trained — skills in ~/skills. New skills are always
created in the primary directory.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request


def skills_dir(request: Request) -> Path:
    """Primary skills directory — where new skills are created."""
    return Path(request.app.state.skills_dir)


def skill_roots(request: Request) -> list[Path]:
    """Ordered skill roots to read from: primary, then ~/skills if present."""
    roots = [skills_dir(request)]
    home = Path.home() / "skills"
    if home.is_dir() and home.resolve() not in {r.resolve() for r in roots}:
        roots.append(home)
    return roots


def resolve_skill_dir(request: Request, skill_name: str) -> Path:
    """Locate an existing skill across the roots, else fall back to primary.

    Returns the first root that already contains ``skill_name``; if none does,
    returns ``primary/skill_name`` so callers' ``is_dir()`` 404 checks still
    fire and any newly created skill lands in the primary directory.
    """
    for root in skill_roots(request):
        candidate = root / skill_name
        if candidate.is_dir():
            return candidate
    return skills_dir(request) / skill_name


def is_skill_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and not path.name.startswith(".")
        and path.name != "__pycache__"
        and (path / "metadata.json").is_file()
    )


def iter_skill_dirs(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return

    for child in sorted(root.iterdir()):
        if is_skill_dir(child):
            yield child


def iter_all_skill_dirs(request: Request) -> Iterator[Path]:
    """Iterate skill dirs across all roots, de-duplicated by name (primary wins)."""
    seen: set[str] = set()
    for root in skill_roots(request):
        for child in iter_skill_dirs(root):
            if child.name in seen:
                continue
            seen.add(child.name)
            yield child
