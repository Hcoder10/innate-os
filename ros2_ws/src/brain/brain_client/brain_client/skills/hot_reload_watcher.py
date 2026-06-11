#!/usr/bin/env python3
"""
File watcher for hot-reloading skills and agents.
Uses watchdog to monitor file changes and triggers reload via ROS service.
"""

import os
import threading
from collections.abc import Callable
from pathlib import Path

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
        """
        self.logger = logger
        self.skills_directories = skills_directories
        self.agents_directories = agents_directories
        self.on_reload = on_reload
        self.debounce_seconds = debounce_seconds
        self.recursive = recursive

        self._observer: Observer | None = None
        self._pending_skills: set[str] = set()
        self._pending_agents: set[str] = set()
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

        self._observer = Observer()

        # Create event handler
        handler = _InternalHandler(
            logger=self.logger,
            on_path_changed=self._on_path_changed,
        )

        # Skills honor self.recursive (physical skills live in subdirs); agents are flat.
        watched_count = 0
        watch_specs = [(d, self.recursive) for d in self.skills_directories] + [
            (d, False) for d in self.agents_directories
        ]
        for directory, recursive in watch_specs:
            if os.path.exists(directory):
                self._observer.schedule(handler, directory, recursive=recursive)
                self.logger.info(f"👁️ Watching for changes: {directory}")
                watched_count += 1

        if watched_count == 0:
            self.logger.warn("No valid directories to watch for hot reload")
            return False

        self._observer.start()
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
        """Called when any watched file changes; resolves it to the skill/agent it belongs to."""
        resolved = self._resolve(file_path)
        if resolved is None:
            return
        item_name, is_skill = resolved

        with self._lock:
            if is_skill:
                self._pending_skills.add(item_name)
            else:
                self._pending_agents.add(item_name)

            # Cancel existing timer and schedule new one
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(self.debounce_seconds, self._execute_reload)
            self._debounce_timer.start()

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
            skills = list(self._pending_skills)
            agents = list(self._pending_agents)
            self._pending_skills.clear()
            self._pending_agents.clear()

        if skills or agents:
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
