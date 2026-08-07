# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Does a workflow's `on.push.paths` filter cover a given file?

Shared by the image-trigger tests (test_assets_image_inputs.py,
test_viewer_bundle_image.py): both defend the same property -- every file that
moves an image's content-addressed tag must also trigger its publish -- so the
glob-matching quirks below must not exist in two copies that can drift.
"""

import fnmatch
from pathlib import Path

import yaml


def push_path_globs(workflow: Path) -> list[str]:
    # `on` parses as the YAML boolean True, not the string "on".
    return yaml.safe_load(workflow.read_text())[True]["push"]["paths"]


def covered(relative_path: str, globs: list[str]) -> bool:
    for glob in globs:
        if fnmatch.fnmatch(relative_path, glob):
            return True
        # `dir/**` matches everything beneath dir; fnmatch alone does not,
        # because it treats `*` as crossing separators inconsistently here.
        if glob.endswith("/**") and relative_path.startswith(glob[:-2]):
            return True
    return False
