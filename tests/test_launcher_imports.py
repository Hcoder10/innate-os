# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Every launcher module imports.

The launcher is a flat package -- modules import each other by bare name off
sys.path, not through a package root -- so a name deleted in `config` is only
discovered when something actually imports the module that wanted it. Nothing
else in the suite imports `runtime`, so an `innate-sim` that cannot start at
all would otherwise leave the tests green.
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "sim" / "launcher"

sys.path.insert(0, str(LAUNCHER))

# Discovered rather than listed: a module added without a line here would
# otherwise go unchecked, which is the gap this file exists to close.
MODULES = sorted(path.stem for path in LAUNCHER.glob("*.py") if not path.stem.startswith("_"))


def test_launcher_has_modules_to_check():
    """Guard the glob: an empty MODULES would make every test below vacuous."""
    assert "config" in MODULES and "runtime" in MODULES, MODULES


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)
