# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Read-only dict compatibility for typed skill-state values.

Old skills use dict-style access (``value["key"]``, ``.get()``, etc.). Typed
values mix this in so that code keeps working. Soft-deprecated via warning;
subclasses define ``_legacy_dict`` and ``_legacy_hint``.
"""

import warnings
from typing import Any


class LegacyMapping:
    _legacy_hint = "the attributes"

    def __getitem__(self, key):
        return self._legacy_mapping()[key]

    def __iter__(self):
        return iter(self._legacy_mapping())

    def __len__(self) -> int:
        return len(self._legacy_mapping())

    def __bool__(self) -> bool:
        # Avoid __len__ truthiness so `if self.<state>:` does not warn.
        return True

    def get(self, key, default=None):
        return self._legacy_mapping().get(key, default)

    def __contains__(self, key) -> bool:
        return key in self._legacy_mapping()

    def keys(self):
        return self._legacy_mapping().keys()

    def items(self):
        return self._legacy_mapping().items()

    def values(self):
        return self._legacy_mapping().values()

    def _legacy_mapping(self) -> dict[str, Any]:
        # FutureWarning (not DeprecationWarning) so skill authors see it on-robot.
        warnings.warn(
            f"dict-style access is deprecated; use {self._legacy_hint}",
            FutureWarning,
            stacklevel=3,
        )
        return self._legacy_dict  # pyright: ignore[reportAttributeAccessIssue] — see class body
