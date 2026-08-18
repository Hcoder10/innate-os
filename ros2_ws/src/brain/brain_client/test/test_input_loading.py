# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for input-device discovery (inputs/loader.py + common/dynamic_loader.py).

Same guarantee the agent loader holds: **nothing silently vanishes**. The load
path is pure Python, so these run under plain pytest against a synthetic inputs
directory in ``tmp_path``.

The alias case is a regression: ``micro_input.py`` imports ``Transcriber =
Callable[[bytes], str]`` to annotate against at runtime, and on py3.10 — what
the robot runs — a subscripted generic still counts as a class, so it reached
``issubclass`` and raised. One imported alias took every device in the file
down with it, and the microphone stopped loading with only a line in the log.
"""

import logging
import textwrap

import pytest

from brain_client.inputs.loader import InputLoader

LOGGER = logging.getLogger("test_input_loading")

DEVICE_SRC = """
from brain_client.inputs.types import InputDevice


class {cls}(InputDevice):
    @property
    def name(self):
        return {name!r}

    def on_open(self):
        pass

    def on_close(self):
        pass
"""


@pytest.fixture
def inputs_dir(tmp_path):
    directory = tmp_path / "inputs"
    directory.mkdir()
    return directory


def load(directory):
    loader = InputLoader(LOGGER)
    return loader.load_from_directories([str(directory)]), loader


def test_discovers_a_device(inputs_dir):
    (inputs_dir / "plain_input.py").write_text(DEVICE_SRC.format(cls="PlainInput", name="plain"))
    found, loader = load(inputs_dir)
    assert sorted(found) == ["plain"]
    assert loader.file_errors == {}


def test_module_level_type_alias_does_not_hide_its_device(inputs_dir):
    # The shape micro_input.py has: an alias imported for runtime annotations,
    # sitting in the same module namespace the loader scans for classes.
    source = "from collections.abc import Callable\n\nTranscriber = Callable[[bytes], str]\n" + DEVICE_SRC.format(
        cls="AliasInput", name="alias"
    )
    (inputs_dir / "alias_input.py").write_text(source)
    found, loader = load(inputs_dir)
    assert sorted(found) == ["alias"], "a module-level generic alias must not void the file"
    assert loader.file_errors == {}


def test_one_broken_file_leaves_the_others(inputs_dir):
    (inputs_dir / "good_input.py").write_text(DEVICE_SRC.format(cls="GoodInput", name="good"))
    (inputs_dir / "broken_input.py").write_text(textwrap.dedent("""
        import a_module_that_does_not_exist  # noqa: F401
    """))
    found, loader = load(inputs_dir)
    assert sorted(found) == ["good"]
    assert any("broken_input.py" in path for path in loader.file_errors)
