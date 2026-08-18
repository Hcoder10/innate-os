# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Regression: a type alias in a device file must not hide its devices.

micro_input.py's ``Transcriber = Callable[[bytes], str]`` reached issubclass on
py3.10 and raised, taking every device in the file with it. Unnoticed for weeks:
the symptom is silence, and py3.11+ does not reproduce it.
"""

import logging

from brain_client.inputs.loader import InputLoader

DEVICE_SRC = """
from collections.abc import Callable

from brain_client.inputs.types import InputDevice

Transcriber = Callable[[bytes], str]


class AliasInput(InputDevice):
    @property
    def name(self):
        return "alias"

    def on_open(self):
        pass

    def on_close(self):
        pass
"""


def test_module_level_type_alias_does_not_hide_its_device(tmp_path):
    (tmp_path / "alias_input.py").write_text(DEVICE_SRC)
    loader = InputLoader(logging.getLogger("test_input_loading"))
    found = loader.load_from_directories([str(tmp_path)])
    assert sorted(found) == ["alias"]
    assert loader.file_errors == {}
