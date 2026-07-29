#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared machinery for the agent and input-device loaders.

Discovers subclasses of a base type in script directories and loads them by
file path. Per-type validation/naming lives in subclasses. Skills no longer
use this (see skills/workspace_import.py); agents and inputs still do.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path
from types import ModuleType

from brain_client.common.script_paths import get_innate_os_root


def class_name_to_snake_case(class_name: str, *, strip_suffixes: tuple[str, ...] = ()) -> str:
    """Convert a CamelCase class name to snake_case, dropping a known suffix first."""
    for suffix in strip_suffixes:
        if class_name.endswith(suffix):
            class_name = class_name[: -len(suffix)]
            break
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", class_name).lower()


def evict_modules_under(directories: list[str]) -> list[str]:
    """Drop cached modules whose source file lives under one of ``directories``.

    Enables reload by forcing the next discovery pass to re-import. Namespace
    packages (no ``__file__``) are skipped. Returns the evicted names.
    """
    roots = [Path(os.path.realpath(d)) for d in directories]
    evicted = []
    for name, module in list(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        path = Path(os.path.realpath(module_file))
        if not any(root in path.parents for root in roots):
            continue
        del sys.modules[name]
        evicted.append(name)
        # Also drop parent.attr bindings or `from pkg import child` stays stale.
        if "." in name:
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None and hasattr(parent, child):
                delattr(parent, child)
    return evicted


class DynamicLoader:
    """Discovers and loads classes of a given base type from script directories.

    Subclasses set :attr:`base_class` (and optionally :attr:`name_suffixes`) and
    implement :meth:`_validate_class` and :meth:`_get_name`.
    """

    base_class: type
    name_suffixes: tuple[str, ...] = ()

    def __init__(self, logger):
        self.logger = logger
        # Load errors by file path; cleared on success. Surfaced as broken entries in the UI.
        self.file_errors: dict[str, str] = {}

    def _iter_candidate_files(self, directory: Path) -> list[Path]:
        """Python files in ``directory`` that may define classes. Override for custom globs."""
        return [f for f in directory.glob("*.py") if f.name != "__init__.py" and not f.name.startswith("_")]

    def _validate_class(self, cls: type) -> bool:
        raise NotImplementedError

    def _get_name(self, cls: type) -> str:
        raise NotImplementedError

    def _make_entry(self, cls: type, file_path: Path):
        """Value stored in the discovery dict for a class. Defaults to ``(class, source_file)``."""
        return (cls, file_path)

    def _entry_module(self, entry) -> str:
        """Module name of a stored entry, for conflict reporting."""
        return entry[0].__module__

    def _exec_module(self, file_path: Path) -> ModuleType | None:
        """Import a module from a file path, exposing ``INNATE_OS_ROOT`` on ``sys.path``.

        Returns the executed module, or ``None`` if it could not be loaded —
        in which case ``file_errors`` records why.
        """
        module_name = file_path.stem
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            self.file_errors[str(file_path)] = "could not load module spec"
            return None

        module = importlib.util.module_from_spec(spec)

        root = str(get_innate_os_root())
        if root not in sys.path:
            sys.path.insert(0, root)
        workspace = str(get_innate_os_root() / "workspace")
        if workspace not in sys.path:
            sys.path.insert(0, workspace)
        parent = str(file_path.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        # Temporary sys.modules entry so annotation resolution works during exec.
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except ModuleNotFoundError as e:
            self.logger.warning(f"Skipping module {module_name}: missing dependency '{e.name or e}'")
            self.file_errors[str(file_path)] = f"missing dependency '{e.name or e}'"
            return None
        except ImportError as e:
            self.logger.warning(f"Skipping module {module_name}: import failed ({e})")
            self.file_errors[str(file_path)] = f"import failed: {e}"
            return None
        except Exception as e:
            self.logger.error(f"Error executing module {module_name}: {e}")
            self.file_errors[str(file_path)] = f"{type(e).__name__}: {e}"
            return None
        finally:
            if previous is not None:
                sys.modules[module_name] = previous
            else:
                sys.modules.pop(module_name, None)
        self.file_errors.pop(str(file_path), None)
        return module

    def _find_classes(self, module: ModuleType) -> list[type]:
        """Validated subclasses of :attr:`base_class` defined in ``module``."""
        found = []
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj is self.base_class or not issubclass(obj, self.base_class):
                continue
            if obj.__module__ != module.__name__:
                continue
            if name.startswith("_"):
                self.logger.debug(f"Skipping helper base class {name} in {module.__name__}")
                continue
            if inspect.isabstract(obj):
                missing = ", ".join(sorted(getattr(obj, "__abstractmethods__", ())))
                self.logger.warning(
                    f"Skipping abstract class {name} in {module.__name__} (unimplemented: {missing}); "
                    "implement the missing methods, or prefix the class with '_' if it is a helper base."
                )
                continue
            if self._validate_class(obj):
                found.append(obj)
            else:
                self.logger.warning(f"Invalid {self.base_class.__name__} class: {name} in {module.__name__}")
        return found

    def _fallback_name(self, cls: type) -> str:
        """Snake_case name derived from the class name, used when instantiation fails."""
        name = class_name_to_snake_case(cls.__name__, strip_suffixes=self.name_suffixes)
        self.logger.debug(f"Using fallback name: {name}")
        return name

    def discover_in_file(self, file_path: Path) -> dict:
        """Returns ``{name: entry}`` for valid classes defined in a single file."""
        result: dict = {}
        module = self._exec_module(file_path)
        if module is None:
            return result
        for cls in self._find_classes(module):
            name = self._get_name(cls)
            result[name] = self._make_entry(cls, file_path)
            self.logger.debug(f"Loaded {self.base_class.__name__}: {name} from {file_path}")
        return result

    def discover_in_directory(self, directory_path: str) -> dict:
        """Returns ``{name: entry}`` for all valid classes found in ``directory_path``."""
        result: dict = {}
        directory = Path(directory_path)
        kind = self.base_class.__name__

        if not directory.exists():
            self.logger.warning(f"{kind} directory does not exist: {directory_path}")
            return result
        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory_path}")
            return result

        self.logger.debug(f"Scanning for {kind} in: {directory_path}")
        for py_file in self._iter_candidate_files(directory):
            try:
                result.update(self.discover_in_file(py_file))
            except Exception as e:
                self.logger.error(f"Error loading {kind} from {py_file}: {e}")

        self.logger.info(f"Discovered {len(result)} {kind} in {directory_path}")
        return result

    def load_from_directories(self, directories: list[str]) -> dict:
        """Returns ``{name: entry}`` across all directories, warning on name conflicts."""
        all_entries: dict = {}
        kind = self.base_class.__name__
        for directory in directories:
            try:
                for name, entry in self.discover_in_directory(directory).items():
                    if name in all_entries:
                        self.logger.warning(
                            f"{kind} name conflict: '{name}' found in both "
                            f"{self._entry_module(all_entries[name])} and "
                            f"{self._entry_module(entry)}. Using the latter."
                        )
                    all_entries[name] = entry
            except Exception as e:
                self.logger.error(f"Error loading {kind} from {directory}: {e}")
        return all_entries
