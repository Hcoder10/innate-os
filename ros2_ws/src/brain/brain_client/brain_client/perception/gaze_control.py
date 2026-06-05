"""Gaze-tracker lifecycle wrapper.

Starts/stops the (lazily imported) ``ROSPersonTracker`` based on brain-active state
and whether the current directive opts into gaze, and pauses/resumes it around
skill execution. The heavy tracker import is deferred so directives that don't use
gaze never pay for it.
"""

from __future__ import annotations


def _tracker_class():
    from brain_client.perception.gaze import ROSPersonTracker

    return ROSPersonTracker


class GazeController:
    def __init__(self, node, state):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._tracker = None

    def update(self) -> None:
        """Start or stop the tracker to match brain state + directive preference."""
        directive = self._state.current_directive
        wants_gaze = (
            self._state.is_brain_active
            and directive is not None
            and hasattr(directive, "uses_gaze")
            and directive.uses_gaze()
        )
        if wants_gaze and self._tracker is None:
            try:
                self._tracker = _tracker_class()(self._node)
                self._tracker.start()
                self._logger.info(f"👁️ Gaze tracker started for directive '{directive.id}'")
            except Exception as e:
                self._logger.error(f"Failed to start gaze tracker: {e}")
                self._tracker = None
        elif not wants_gaze and self._tracker is not None:
            self.stop()

    def stop(self) -> None:
        if self._tracker is not None:
            try:
                self._tracker.stop()
                self._logger.info("👁️ Gaze tracker stopped")
            except Exception as e:
                self._logger.error(f"Error stopping gaze tracker: {e}")
            self._tracker = None

    def pause(self) -> None:
        if self._tracker is not None and self._tracker.is_running:
            self._tracker.stop()
            self._logger.debug("👁️ Gaze paused for skill execution")

    def resume(self) -> None:
        if self._tracker is not None and not self._tracker.is_running:
            self._tracker.start()
            self._logger.debug("👁️ Gaze resumed after skill execution")
