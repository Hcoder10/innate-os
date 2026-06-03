#!/usr/bin/env python3
"""
Dynamic Input Device Loader

This module provides functionality to dynamically discover and load input device classes
from specified directories. Similar to primitive_loader.py and directive_loader.py.
"""

from pathlib import Path

from brain_client.common.dynamic_loader import DynamicLoader
from brain_client.inputs.types import InputDevice


class InputLoader(DynamicLoader):
    """
    Dynamically loads input device classes from specified directories.

    Sets logger and proxy on each loaded input device for accessing
    external services (TTS, STT, etc.)
    """

    base_class = InputDevice
    name_suffixes = ("Input",)

    def __init__(self, logger, proxy=None):
        """
        Initialize the input loader.

        Args:
            logger: Logger instance
            proxy: ProxyClient instance to set on input devices
        """
        super().__init__(logger)
        self._proxy = proxy

    def _iter_candidate_files(self, directory: Path) -> list[Path]:
        # Look for Python files ending in _input.py
        return [
            f
            for f in directory.glob("*_input.py")
            if f.name not in ["__init__.py", "input_types.py"] and not f.name.startswith("_")
        ]

    def _make_entry(self, cls: type[InputDevice], file_path: Path) -> type[InputDevice]:
        # Input devices are stored by class only; source file is not tracked.
        return cls

    def _entry_module(self, entry: type[InputDevice]) -> str:
        return entry.__module__

    def _validate_class(self, input_class: type[InputDevice]) -> bool:
        """
        Validates that an input device class is properly implemented.

        Args:
            input_class: The input device class to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            # Check that required abstract methods are implemented
            required_methods = ["name", "on_open", "on_close"]
            for method_name in required_methods:
                if not hasattr(input_class, method_name):
                    self.logger.error(f"Input device {input_class.__name__} missing required method: {method_name}")
                    return False

            # Check that name is a property
            if not isinstance(getattr(input_class, "name", None), property):
                self.logger.error(f"Input device {input_class.__name__} 'name' must be a property")
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating input device {input_class.__name__}: {e}")
            return False

    def _get_name(self, input_class: type[InputDevice]) -> str:
        """Gets the input device name by creating a temporary instance."""
        try:
            return input_class().name
        except Exception as e:
            self.logger.debug(f"Could not get name from input device {input_class.__name__}: {e}")
            return self._fallback_name(input_class)

    def create_input_instances(self, input_classes: dict[str, type[InputDevice]], logger) -> dict[str, InputDevice]:
        """
        Create instances of input device classes and set logger/proxy.

        Args:
            input_classes: Dictionary of input device name to class mappings
            logger: Logger instance to set on input devices

        Returns:
            Dictionary mapping input device names to their instances
        """
        input_instances = {}

        for input_name, input_class in input_classes.items():
            try:
                # Create instance and set logger/proxy
                input_instance = input_class()
                input_instance.set_logger(logger)
                input_instance.set_proxy(self._proxy)
                input_instances[input_name] = input_instance
                self.logger.debug(
                    f"Created input device instance: {input_name} (proxy: {'yes' if self._proxy else 'no'})"
                )
            except Exception as e:
                self.logger.error(f"Error creating input device instance {input_name}: {e}")

        return input_instances
