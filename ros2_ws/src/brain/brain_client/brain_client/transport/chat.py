# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Chat history, chat-out publishing, task-status publishing, and TTS.

Consolidates the ``{"sender", "text", "timestamp"}`` chat-entry dict and the
task-status payload that were copy-pasted across the old node. Owns the chat
history list so no other component needs to.
"""

from __future__ import annotations

import json
import re
import time

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


class ChatManager:
    def __init__(self, logger, chat_out_pub, task_status_pub, tts_handler=None):
        self._logger = logger
        self._chat_out_pub = chat_out_pub
        self._task_status_pub = task_status_pub
        self._tts_handler = tts_handler
        self.history: list[dict] = []

    @staticmethod
    def entry(sender: str, text: str) -> dict:
        return {"sender": sender, "text": text, "timestamp": time.time()}

    def emit(self, sender: str, text: str, speak: bool | None = None) -> None:
        """Append a chat entry, publish it, and (for robot speech) speak it.

        ``speak`` defaults to True only for the ``"robot"`` sender, matching the
        old behaviour where thoughts/anticipation were published but not spoken.
        """
        chat_entry = self.entry(sender, text)
        self.history.append(chat_entry)
        self._logger.debug(f"chat_out: {chat_entry}")
        from std_msgs.msg import String  # deferred: keeps the module importable without ROS

        self._chat_out_pub.publish(String(data=json.dumps(chat_entry)))

        if speak is None:
            speak = sender == "robot"
        if speak and text and text.strip():
            self.speak(text)

    def emit_system(self, text: str) -> None:
        """Publish a system message (never spoken)."""
        self.emit("system", text, speak=False)

    def emit_thoughts(self, thoughts: str) -> None:
        """Publish a thought summary, trimmed — the panel wants a glimpse, not a log."""
        if len(thoughts) > 600:
            thoughts = thoughts[:600].rsplit(" ", 1)[0] + " …"
        self.emit("robot_thoughts", thoughts, speak=False)

    def publish_task_status(
        self,
        primitive_name: str,
        primitive_id: str | None,
        status: str,
        skill_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Publish a local task-status update for the controller-app UI."""
        payload = {
            "primitive_name": primitive_name,
            "primitive_id": primitive_id,
            "skill_name": primitive_name,
            "skill_id": skill_id or primitive_id,
            "status": status,
            "timestamp": time.time(),
        }
        if reason:
            payload["reason"] = reason
        from std_msgs.msg import String  # deferred: keeps the module importable without ROS

        self._task_status_pub.publish(String(data=json.dumps(payload)))

    def history_json(self) -> str:
        return json.dumps(self.history)

    def clear(self) -> None:
        self.history = []

    def speak(self, text: str, replace_pending: bool = False) -> None:
        if self._tts_handler is not None:
            self._tts_handler.speak_text_async(text, replace_pending=replace_pending)

    def stream_speech(self) -> SpeechStreamer:
        return SpeechStreamer(self)


class SpeechStreamer:
    """Feeds a streaming reply to TTS one sentence at a time, so the robot
    starts talking at the first sentence boundary instead of the last."""

    def __init__(self, chat: ChatManager):
        self._chat = chat
        self._buffer = ""
        self._muted = False
        self.spoke = False

    def feed(self, text: str) -> None:
        self._buffer += text
        *sentences, self._buffer = _SENTENCE_END.split(self._buffer)
        for sentence in sentences:
            self._say(sentence)

    def flush(self) -> None:
        self._say(self._buffer)
        self._buffer = ""

    def _say(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence.startswith("Calling tool"):  # leaked tool narration, never speech
            self._muted = True
        if self._muted or not re.search(r"[a-zA-Z0-9]", sentence):
            return
        # The first sentence supersedes any stale queued utterances; the rest
        # of the reply queues in order behind it.
        self._chat.speak(sentence, replace_pending=not self.spoke)
        self.spoke = True
