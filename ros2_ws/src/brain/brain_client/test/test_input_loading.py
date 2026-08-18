# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Regression: a type alias in a device file must not hide its devices.

``micro_input.py`` imports ``Transcriber = Callable[[bytes], str]`` to annotate
against at runtime, and on py3.10 — what the robot runs — a subscripted generic
still counts as a class, so it reached ``issubclass``, which raised. One member
took every device in the file with it, and the microphone stopped loading with
a single cryptic line in the log. It went unnoticed for weeks because the
symptom is silence, and because py3.11+ does not reproduce it.
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
