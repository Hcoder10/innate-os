#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
File watcher for hot-reloading skills and agents.
Uses watchdog to monitor file changes and triggers reload via ROS service.
"""

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

# watchdog is optional: the robot falls back to the reload service when it is
# absent. The fallbacks below rebind the names, which a type checker cannot
# use as types — so under TYPE_CHECKING it reads the real ones (start()
# refuses to run unless WATCHDOG_AVAILABLE, so they are always real at use).
if TYPE_CHECKING:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
else:
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        WATCHDOG_AVAILABLE = True
    except ImportError:
        WATCHDOG_AVAILABLE = False
        Observer = None
        FileSystemEventHandler = object


class HotReloadWatcher:
    """
    Watches skills and agents directories for file changes.
    Triggers reload callbacks when Python files are modified.
    """

    def __init__(
        self,
        logger,
        skills_directories: list[str],
        agents_directories: list[str],
        on_reload: Callable[[list[str], list[str]], None],
        debounce_seconds: float = 1.0,
        recursive: bool = False,
        workspace_roots: list[str] | None = None,
    ):
        """
        Initialize the hot reload watcher.

        Args:
            logger: ROS logger instance
            skills_directories: List of skill directories to watch
            agents_directories: List of agent directories to watch
            on_reload: Callback function that takes (skill_names, agent_names)
            debounce_seconds: Time to wait before triggering reload after last change
            recursive: Watch subdirectories too. Required for physical skills, which
                live in a per-skill subdirectory (``<root>/<skill>/metadata.json`` and
                assets); a change to any file in there reloads that skill. Agents are
                flat ``.py`` files, so they watch non-recursively.
            workspace_roots: Roots holding skill packages (``workspace/``), watched
                recursively. Any ``.py`` change under one reloads everything — a
                package's modules import each other freely, so no single skill owns
                an edit — and because it is one recursive watch on the root, a pack
                dropped in after boot (``cp -r john_skills workspace/``) is picked up
                without a restart.
        """
        self.logger = logger
        self.skills_directories = skills_directories
        self.agents_directories = agents_directories
        self.workspace_roots = workspace_roots or []
        self.on_reload = on_reload
        self.debounce_seconds = debounce_seconds
        self.recursive = recursive

        self._observer: Observer | None = None
        self._pending_skills: set[str] = set()
        self._pending_agents: set[str] = set()
        self._pending_reload_all = False
        self._lock = threading.Lock()
        self._debounce_timer: threading.Timer | None = None
        self._running = False

    def start(self):
        """Start watching for file changes."""
        if not WATCHDOG_AVAILABLE:
            self.logger.warn(
                "⚠️ watchdog package not installed. Hot reload file watching disabled. "
                "Install with: pip install watchdog"
            )
            return False

        if self._running:
            self.logger.warn("Hot reload watcher already running")
            return True

        observer = Observer()
        self._observer = observer

        # Create event handler
        handler = _InternalHandler(
            logger=self.logger,
            on_path_changed=self._on_path_changed,
        )

        # Skills honor self.recursive (physical skills live in subdirs); agents are flat.
        # A directory already inside a workspace root is skipped: the recursive
        # root watch delivers its events, and scheduling both would report every
        # change twice. It still resolves through skills_directories, so a
        # physical skill's non-.py files map to their skill (see _record_change).
        watched_count = 0
        watch_specs = (
            [(d, self.recursive) for d in self.skills_directories if not self._inside_workspace_root(d)]
            + [(d, False) for d in self.agents_directories if not self._inside_workspace_root(d)]
            + [(d, True) for d in self.workspace_roots]
        )
        for directory, recursive in watch_specs:
            if os.path.exists(directory):
                observer.schedule(handler, directory, recursive=recursive)
                self.logger.info(f"👁️ Watching for changes: {directory}")
                watched_count += 1

        if watched_count == 0:
            self.logger.warn("No valid directories to watch for hot reload")
            return False

        observer.start()
        self._running = True
        self.logger.info(f"🔥 Hot reload watcher started ({watched_count} directories)")
        return True

    def stop(self):
        """Stop watching for file changes."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None

        self._running = False
        self.logger.info("Hot reload watcher stopped")

    def _on_path_changed(self, file_path: str):
        """Called when any watched file changes; records what it reloads and arms the timer."""
        with self._lock:
            if not self._record_change(file_path):
                return

            # Cancel existing timer and schedule new one
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(self.debounce_seconds, self._execute_reload)
            self._debounce_timer.start()

    def _record_change(self, file_path: str) -> bool:
        """Note what a changed file reloads; False if it's noise. Caller holds the lock."""
        if self._is_workspace_change(file_path):
            # Workspace packages import each other freely, so no single skill
            # owns an edit: empty lists mean "reload everything".
            self._pending_reload_all = True
            return True
        resolved = self._resolve(file_path)
        if resolved is None:
            return False
        item_name, is_skill = resolved
        target = self._pending_skills if is_skill else self._pending_agents
        target.add(item_name)
        return True

    def _inside_workspace_root(self, directory: str) -> bool:
        """True when a recursive workspace-root watch already covers ``directory``."""
        path = Path(os.path.realpath(directory))
        for root in self.workspace_roots:
            resolved = Path(os.path.realpath(root))
            if path == resolved or resolved in path.parents:
                return True
        return False

    def _is_workspace_change(self, file_path: str) -> bool:
        """True for a ``.py`` change under a watched workspace root.

        Skipping ``__pycache__`` is what stops a reload loop: the reload this
        triggers writes ``.pyc`` files back under the same root.
        """
        path = Path(os.path.realpath(file_path))
        if path.suffix != ".py":
            return False
        for root in self.workspace_roots:
            try:
                rel = path.relative_to(Path(os.path.realpath(root)))
            except ValueError:
                continue
            return not any(part == "__pycache__" or part.startswith(".") for part in rel.parts)
        return False

    def _resolve(self, file_path: str) -> tuple[str, bool] | None:
        """Map a changed file to ``(item_name, is_skill)``, or ``None`` if it's noise.

        Skills win over agents when a path matches both (a given watcher only ever
        schedules one of the two, so in practice there's no overlap).

        Both sides are realpath-normalized: watchdog may report resolved paths while
        a configured root is a symlink (e.g. macOS /var -> /private/var), which would
        otherwise break the relative_to matching and silently drop the event.
        """
        path = Path(os.path.realpath(file_path))
        for root in self.skills_directories:
            name = _skill_name_for(Path(os.path.realpath(root)), path)
            if name is not None:
                return name, True
        for root in self.agents_directories:
            name = _flat_py_name_for(Path(os.path.realpath(root)), path)
            if name is not None:
                return name, False
        return None

    def _execute_reload(self):
        """Execute pending reloads."""
        with self._lock:
            reload_all = self._pending_reload_all
            skills = list(self._pending_skills)
            agents = list(self._pending_agents)
            self._pending_reload_all = False
            self._pending_skills.clear()
            self._pending_agents.clear()

        if not reload_all and not skills and not agents:
            return

        if reload_all:
            # Empty lists mean "reload everything", which also covers whatever
            # individual skills changed inside the same debounce window.
            skills, agents = [], []
            self.logger.info("🔄 Hot reload triggered - shared helper changed, reloading all skills")
        else:
            self.logger.info(f"🔄 Hot reload triggered - skills: {skills}, agents: {agents}")

        try:
            self.on_reload(skills, agents)
        except Exception as e:
            self.logger.error(f"Hot reload failed: {e}")


def _flat_py_name_for(root: Path, path: Path) -> str | None:
    """A loadable ``.py`` file sitting directly in ``root`` -> its stem, else ``None``.

    Used for agents and for top-level (code) skills, which are flat ``.py`` files.
    """
    if path.parent != root:
        return None
    if path.suffix != ".py" or path.name == "__init__.py" or path.name.startswith("_"):
        return None
    return path.stem


def _skill_name_for(root: Path, path: Path) -> str | None:
    """Map a changed file under a skills ``root`` to the skill it belongs to.

    - ``root/foo.py``            -> ``"foo"``  (code skill; ``.py`` only)
    - ``root/foo/metadata.json`` -> ``"foo"``  (physical skill; any file type)
    - ``root/foo/assets/x.png``  -> ``"foo"``

    Returns ``None`` for files outside ``root`` and for editor/build noise
    (``__pycache__``, dotfiles, temp/swap files) so reloads don't loop on the
    ``.pyc`` written during a reload.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) == 1:
        return _flat_py_name_for(root, path)  # top-level code skill
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return None  # hidden/build dir anywhere in the path (.git, __pycache__, ...)
    if parts[0].startswith("_") or _is_transient(path.name):
        return None
    return parts[0]  # physical-skill subdirectory


def _is_transient(filename: str) -> bool:
    """Editor/atomic-write scratch files that shouldn't drive a reload on their own.

    Dotfiles are already filtered by the per-part hidden-path check in
    ``_skill_name_for`` before this runs, so only the temp/swap suffixes are checked here.
    """
    return filename.endswith((".tmp", ".swp", "~"))


class _InternalHandler(FileSystemEventHandler):
    """Internal handler for watchdog events; forwards raw paths to the watcher's resolver."""

    def __init__(self, logger, on_path_changed: Callable[[str], None]):
        super().__init__()
        self.logger = logger
        self.on_path_changed = on_path_changed

    def on_modified(self, event):
        if not event.is_directory:
            self.on_path_changed(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self.on_path_changed(event.src_path)

    def on_deleted(self, event):
        # Deleting a skill's .py or a physical skill's file should reload it so the
        # catalog drops the stale entry.
        if not event.is_directory:
            self.on_path_changed(event.src_path)

    def on_moved(self, event):
        # Atomic saves arrive as a rename onto the target, not a modify.
        if not event.is_directory and event.dest_path:
            self.on_path_changed(event.dest_path)
