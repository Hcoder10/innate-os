"""Architectural guard: the pure layer must never import ``rclpy``.

The whole point of the pure modules is that they are unit-testable without a ROS
runtime. This test enforces that boundary so it cannot silently rot — if someone
adds an ``import rclpy`` (directly or transitively) to a module listed below, this
fails. Each import runs in a fresh interpreter so a ROS import elsewhere in the
test session can't mask a violation.
"""

import subprocess
import sys

import pytest

PURE_MODULES = [
    "brain_client.perception.pose",
    "brain_client.perception.image_codec",
    "brain_client.navigation.payload",
    "brain_client.skills.registry",
    "brain_client.transport.messages",
    "brain_client.transport.ws_config",
    "brain_client.core.config",
]


@pytest.mark.parametrize("module", PURE_MODULES)
def test_pure_module_does_not_import_rclpy(module):
    code = f"import {module}; import sys; assert 'rclpy' not in sys.modules, 'rclpy leaked into {module}'"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
