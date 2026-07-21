# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot-persisted favorite skills.

The user's starred skills, stored on the robot so every client UI (webapp,
mobile app) sees the same list. ROS-free by design: the skills server wires
this store to the ``/brain/favorite_skills`` (latched broadcast) and
``/brain/set_favorite_skills`` (full-list replace) topics; clients that star a
skill publish the whole updated list and the broadcast echo confirms it.

Ids are stored as given — a favorite whose skill is mid-reload (or on a
temporarily unplugged code path) must survive, so validation against the live
roster is the consumer's job, not the store's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class FavoriteSkillsStore:
    """Ordered, deduped favorite skill ids persisted as a small JSON file."""

    def __init__(self, path: Path, logger):
        self._path = Path(path)
        self._logger = logger
        self._ids: list[str] = self._load()

    @property
    def ids(self) -> list[str]:
        return list(self._ids)

    def replace(self, ids) -> list[str]:
        """Replace the whole list (the client-toggle contract), persist, return it.

        A payload that isn't a list at all is ignored — persisting it would
        wipe the stored favorites, and only `[]` (an explicit unstar-all) may
        do that. Within a list, non-strings are dropped and duplicates
        collapse to their first position; malformed input therefore degrades
        to "some stars ignored", never to a crash or a wiped file.
        """
        if not isinstance(ids, list):
            self._logger.warning(
                f"Ignoring non-list favorite skills payload ({type(ids).__name__}); keeping current list"
            )
            return self.ids
        self._ids = self._sanitize(ids)
        self._save()
        return self.ids

    @staticmethod
    def _sanitize(ids) -> list[str]:
        if not isinstance(ids, list):
            return []
        seen = set()
        cleaned = []
        for skill_id in ids:
            if not isinstance(skill_id, str):
                continue
            skill_id = skill_id.strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            cleaned.append(skill_id)
        return cleaned

    def _load(self) -> list[str]:
        try:
            payload = json.loads(self._path.read_text())
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as e:
            self._logger.warning(f"Could not read favorite skills from {self._path}: {e}; starting empty")
            return []
        return self._sanitize(payload.get("skills") if isinstance(payload, dict) else payload)

    def _save(self) -> None:
        """Atomic write (tmp + os.replace) — a crash mid-save never truncates the file."""
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps({"skills": self._ids}, indent=2) + "\n")
            os.replace(tmp_path, self._path)
        except OSError as e:
            self._logger.error(f"Could not persist favorite skills to {self._path}: {e}")
            tmp_path.unlink(missing_ok=True)
