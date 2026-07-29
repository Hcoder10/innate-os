# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Sibling helper imports and load-error capture in the dynamic loader.

The custom/innate skill experience is symmetric: a helper ``.py`` next to a
skill file is importable by bare name (the loader puts the file's directory on
``sys.path``, like ``python script.py`` would), and a file that fails to
execute records its error in ``file_errors`` instead of silently vanishing —
the catalog turns those into visible "broken skill" roster entries.

Tests drive the DynamicLoader base directly with a stub base class; no ROS,
no h5py. Part of the fast (no-ROS) pytest bucket in ci/run_integration_tests.sh.
"""

import logging
import sys
import textwrap

import pytest

from brain_client.common.dynamic_loader import DynamicLoader, evict_modules_under

LOGGER = logging.getLogger("dynamic_loader_helpers_test")


class _Base:
    pass


class _StubLoader(DynamicLoader):
    base_class = _Base

    def _validate_class(self, cls: type) -> bool:
        return True

    def _get_name(self, cls: type) -> str:
        return cls.__name__


@pytest.fixture
def loader():
    return _StubLoader(LOGGER)


@pytest.fixture
def _clean_modules():
    """Drop modules the tests import so runs don't leak into each other."""
    yield
    for name in ("my_helpers", "helper_pkg", "helper_pkg.inner"):
        sys.modules.pop(name, None)


pytestmark = pytest.mark.usefixtures("_clean_modules")


def _write_skill(path, body=""):
    content = textwrap.dedent(
        """
        from test_dynamic_loader_helpers import _Base

        class Thing(_Base):
            pass
        """
    )
    path.write_text(f"{body}\n{content}" if body else content)


def test_sibling_helper_imports_by_bare_name(tmp_path, loader):
    (tmp_path / "my_helpers.py").write_text("VALUE = 41\n")
    skill_file = tmp_path / "uses_helper.py"
    _write_skill(skill_file, body="import my_helpers\nassert my_helpers.VALUE == 41")

    discovered = loader.discover_in_file(skill_file)

    assert "Thing" in discovered
    assert str(skill_file) not in loader.file_errors


def test_broken_file_records_its_error(tmp_path, loader):
    bad = tmp_path / "broken_skill.py"
    bad.write_text("import does_not_exist_anywhere\n")

    assert loader.discover_in_file(bad) == {}
    assert "does_not_exist_anywhere" in loader.file_errors[str(bad)]


def test_syntax_error_records_its_error(tmp_path, loader):
    bad = tmp_path / "broken_skill.py"
    bad.write_text("def nope(:\n")

    assert loader.discover_in_file(bad) == {}
    assert "SyntaxError" in loader.file_errors[str(bad)]


def test_fixing_the_file_clears_the_error(tmp_path, loader):
    path = tmp_path / "flaky_skill.py"
    path.write_text("raise RuntimeError('boom')\n")
    loader.discover_in_file(path)
    assert "boom" in loader.file_errors[str(path)]

    _write_skill(path)
    discovered = loader.discover_in_file(path)

    assert "Thing" in discovered
    assert str(path) not in loader.file_errors


def test_evict_modules_under_drops_cached_helpers(tmp_path, loader):
    """A reloaded skill must re-import an edited helper, not the cached copy."""
    helper = tmp_path / "my_helpers.py"
    helper.write_text("VALUE = 'before'\n")
    skill_file = tmp_path / "uses_helper.py"
    _write_skill(skill_file, body="import my_helpers")
    loader.discover_in_file(skill_file)
    assert sys.modules["my_helpers"].VALUE == "before"

    helper.write_text("VALUE = 'after'\n")
    evicted = evict_modules_under([str(tmp_path)])
    assert "my_helpers" in evicted

    loader.discover_in_file(skill_file)
    assert sys.modules["my_helpers"].VALUE == "after"


def test_evict_modules_under_ignores_modules_elsewhere(tmp_path):
    before = set(sys.modules)
    assert evict_modules_under([str(tmp_path / "empty")]) == []
    assert set(sys.modules) == before


def test_evict_drops_parent_package_attribute(tmp_path, loader):
    """`from pkg import helper` binds the module on the parent package; the
    stale attribute must go too or that import form keeps the old module."""
    pkg = tmp_path / "helper_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "inner.py").write_text("VALUE = 'before'\n")
    skill_file = tmp_path / "uses_pkg.py"
    _write_skill(skill_file, body="from helper_pkg import inner\nassert inner.VALUE == 'before'")
    loader.discover_in_file(skill_file)

    (pkg / "inner.py").write_text("VALUE = 'after'\n")
    evicted = evict_modules_under([str(tmp_path)])

    assert "helper_pkg.inner" in evicted
    assert "helper_pkg" in evicted  # its __init__.py lives under tmp_path too
    _write_skill(skill_file, body="from helper_pkg import inner\nassert inner.VALUE == 'after'")
    assert "Thing" in loader.discover_in_file(skill_file)
