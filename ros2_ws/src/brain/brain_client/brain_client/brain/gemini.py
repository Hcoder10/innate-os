# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini inference for the local brain, over the native Gemini REST API.

The brain reaches Gemini through the Innate proxy (service ``gemini`` — the
proxy holds the upstream key and passes native ``:streamGenerateContent``
calls through untouched; the robot authenticates with its service key) or directly
against ``generativelanguage.googleapis.com`` when ``GEMINI_API_KEY`` is set.
Both speak the same wire format: JSON contents/parts, inline JPEG images,
function tools.

The native API — unlike the OpenAI-compatible layer — returns *thought
summaries* (parts flagged ``thought: true``), which the agent surfaces in the
app chat as ``robot_thoughts``, matching the old cloud brain's thoughts channel.

Everything model-facing lives here: skill metadata -> tool declarations, the
bounded content history (old camera frames are pruned so requests stay small),
and distilling a response into a plain :class:`Decision` the agent loop acts on.

Threading contract: :meth:`GeminiSession.generate` is the only blocking network
call and only *reads* the history, so the agent awaits it on a worker thread
(``asyncio.to_thread``). All history mutation (:meth:`absorb`,
:meth:`add_tool_outcomes`) happens in the agent's coroutine, strictly between
generate calls, and :meth:`clear` only runs while the loop task is stopped —
there is no concurrent access to a session by construction, not by lock.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

STOP_SKILL = "stop_current_skill"
WAIT = "wait"
GO_TO_POINT_IN_VIEW = "go_to_point_in_view"

# An explicit no-op keeps idle turns clean: without it, models tend to emit
# placeholder text ("[]", "Empty response") rather than returning nothing.
_WAIT_DECLARATION = {
    "name": WAIT,
    "description": "Do nothing until the next update. Use when there is nothing new to do or say.",
}

# Visual navigation grounding: the model points at a floor pixel and the robot
# projects it into a local navigation goal (brain/grounding.py). Declared only
# when navigate_to_position is among the active skills — it is the actuator.
_GO_TO_POINT_IN_VIEW_DECLARATION = {
    "name": GO_TO_POINT_IN_VIEW,
    "description": (
        "Drive toward a point you can see in the CURRENT camera frame. Give normalized image "
        "coordinates (0-1000) of a point ON THE FLOOR: y from the top, x from the left. For an "
        "object, point at the floor at its base. The robot drives to about 0.35 m short of that "
        "spot and turns to face it. Prefer this over navigate_to_position for anything you can "
        "see. Far targets are approached in capped steps — call it again after arriving."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "y": {"type": "INTEGER", "description": "0-1000 from image top"},
            "x": {"type": "INTEGER", "description": "0-1000 from image left"},
        },
        "required": ["y", "x"],
    },
}

PROXY_SERVICE = "gemini"
DIRECT_BASE_URL = "https://generativelanguage.googleapis.com"
STREAM_PATH = "/v1beta/models/{model}:streamGenerateContent?alt=sse"

_FRAME_REMOVED = {"text": "[older camera frame removed]"}
_WRIST_FRAME_REMOVED = {"text": "[older wrist camera frame removed]"}

# Skill input "type" strings (python annotation names from skill introspection)
# -> Gemini schema types. Anything else is passed as a string with the expected
# type noted in the description.
_SCHEMA_TYPES = {"float": "NUMBER", "int": "INTEGER", "str": "STRING", "bool": "BOOLEAN"}


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = ""


@dataclass
class Decision:
    """What the model wants the robot to do this turn."""

    speech: str | None = None
    thoughts: str | None = None
    calls: list[ToolCall] = field(default_factory=list)


def tool_name(skill_name: str) -> str:
    """Skill name -> valid function name.

    Gemini requires function names to start with a letter or underscore — a
    digit-leading skill ("3d_scan") would 400 every request while active.
    """
    name = re.sub(r"[^a-zA-Z0-9_.\-]", "_", skill_name) or "skill"
    if not re.match(r"[a-zA-Z_]", name):
        name = "_" + name
    return name[:64]


def assign_tool_names(skills: list[dict]) -> list[tuple[str, dict]]:
    """Give every skill a unique function name, in roster order.

    Sanitizing/truncating can make two skill names collide (and a skill can
    shadow a built-in tool); colliding names get a numeric suffix so a call
    never silently dispatches to the wrong skill.
    """
    taken = {STOP_SKILL, WAIT, GO_TO_POINT_IN_VIEW}
    named = []
    for meta in skills:
        base = name = tool_name(meta["name"])
        counter = 2
        while name in taken:
            suffix = f"_{counter}"
            name = base[: 64 - len(suffix)] + suffix
            counter += 1
        taken.add(name)
        named.append((name, meta))
    return named


def build_tools(
    named_skills: list[tuple[str, dict]],
    running_skill_name: str | None,
    *,
    can_go_to_point_in_view: bool = False,
    user_spoke: bool = False,
) -> list[dict]:
    """One function declaration per available skill, in a native tools block.

    ``named_skills`` is :func:`assign_tool_names`'s output — the caller derives
    its dispatch map from the same pairs, so the declared names and the
    dispatched names can never diverge.

    While a skill runs, the robot's only action is stopping it, so the
    declarations collapse. The shape depends on whether the user just spoke:
    offered any no-op tool, the model calls it and goes silent, so a turn
    carrying a user message gets stop_current_skill ALONE — plain text becomes
    the reply channel, and the description steers stop away from questions.
    """
    if running_skill_name is not None:
        stop = {
            "name": STOP_SKILL,
            "description": f"Abort the currently running skill ({running_skill_name}). "
            + (
                "Only when the user asks you to stop or switch task, or the skill is clearly "
                "failing. Questions and conversation are NOT reasons to stop — answer those "
                "in text and let the skill continue."
                if user_spoke
                else "Use when it is clearly failing, no longer makes sense, or the user asks for something else."
            ),
        }
        return [{"functionDeclarations": [stop] if user_spoke else [stop, _WAIT_DECLARATION]}]
    declarations = [_declaration(name, meta) for name, meta in named_skills]
    if can_go_to_point_in_view:
        declarations.append(_GO_TO_POINT_IN_VIEW_DECLARATION)
    declarations.append(_WAIT_DECLARATION)
    return [{"functionDeclarations": declarations}]


def _declaration(name: str, meta: dict) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param_name, spec in (meta.get("inputs") or {}).items():
        properties[param_name] = _param_schema(spec if isinstance(spec, dict) else {})
        if isinstance(spec, dict) and spec.get("required"):
            required.append(param_name)
    declaration = {"name": name, "description": meta.get("guidelines") or meta["name"]}
    if properties:
        declaration["parameters"] = {"type": "OBJECT", "properties": properties, "required": required}
    return declaration


def _param_schema(spec: dict) -> dict:
    declared_type = str(spec.get("type", "any"))
    schema: dict = {"type": _SCHEMA_TYPES.get(declared_type)}
    notes = []
    if schema["type"] is None:
        notes.append(f"type: {declared_type}")
        schema["type"] = "STRING"
    if "default" in spec:
        notes.append(f"default: {spec['default']}")

    enum_values = spec.get("enum")
    if enum_values and all(isinstance(v, str) for v in enum_values):
        schema["type"] = "STRING"
        schema["enum"] = list(enum_values)
    elif enum_values:
        notes.append(f"one of: {enum_values}")

    if notes:
        schema["description"] = ", ".join(notes)
    return schema


# ---------- transports (callable: (model, body) -> iterator of response chunks) ----------


def proxy_transport(proxy):
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""

    def stream(model: str, body: dict):
        endpoint = STREAM_PATH.format(model=model)
        with proxy.request_stream(PROXY_SERVICE, endpoint, json=body) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"gemini via proxy: HTTP {resp.status_code}: {resp.read()[:200]!r}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def pick_transport(proxy):
    """The way to reach Gemini: the Innate proxy (managed) or GEMINI_API_KEY (dev)."""
    if proxy is not None and proxy.is_available():
        return proxy_transport(proxy), "innate-proxy"
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return direct_transport(api_key), "gemini-direct"
    return None, "unconfigured"


def direct_transport(api_key: str):
    """Reach Google's Gemini API directly with GEMINI_API_KEY."""
    # One client for the process: reuses the TLS connection across turns
    # instead of a fresh handshake per generate call. Single-threaded use by
    # construction (one turn at a time on the agent's worker thread).
    client = httpx.Client(headers={"x-goog-api-key": api_key}, timeout=90.0)

    def stream(model: str, body: dict):
        url = DIRECT_BASE_URL + STREAM_PATH.format(model=model)
        with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise RuntimeError(f"gemini direct: HTTP {resp.status_code}: {resp.text[:200]}")
            yield from _sse_chunks(resp.iter_lines())

    return stream


def _sse_chunks(lines):
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[len("data: ") :])


class GeminiSession:
    """A bounded conversation with Gemini: one generate() per agent turn."""

    def __init__(self, transport, *, model: str, thinking_level: str, max_history: int, max_image_turns: int):
        self._transport = transport
        self._model = model
        self._thinking_level = thinking_level
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        self._history: list[dict] = []
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (content, part indexes) — absorbing a newer set prunes these.
        self._latest_only_turn: tuple[dict, list[int]] | None = None
        # Observability tap: called with the exact request body just before it
        # goes on the wire (from generate's thread). The body must be treated
        # as read-only — it shares structure with the live history.
        self.on_request: Callable[[dict], None] | None = None

    def clear(self) -> None:
        self._history = []
        self._latest_only_turn = None

    @property
    def history_len(self) -> int:
        return len(self._history)

    @property
    def image_turn_count(self) -> int:
        """History turns still carrying camera frames (pruning keeps the newest few)."""
        return sum(
            1 for c in self._history if c.get("role") == "user" and any("inlineData" in p for p in c.get("parts") or [])
        )

    @staticmethod
    def user_message(text: str, images: list[bytes]) -> dict:
        parts: list[dict] = [{"text": text}]
        for jpeg in images:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
        return {"role": "user", "parts": parts}

    def generate(
        self,
        user_message: dict,
        tools: list[dict],
        system: str,
        on_speech=None,
        *,
        latest_only_images: list[int] | None = None,
    ) -> dict:
        """Blocking network call — safe on a worker thread (history is only read).

        The reply streams in; every plain-text delta is handed to ``on_speech``
        as it arrives, which is what lets the robot start talking at the first
        sentence boundary. Returns the fully assembled response.

        ``latest_only_images`` mirrors :meth:`absorb`'s: when this message
        carries wrist frames, the previous turn's copies are masked out of the
        request here — absorb's durable prune runs only after the response, so
        without this every request would ship two wrist frames. History is
        masked via shallow copies, never mutated (an abandoned turn's orphaned
        request may still be reading it).
        """
        contents = [*self._history, user_message]
        if latest_only_images and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = {
                **stale,
                "parts": [
                    dict(_WRIST_FRAME_REMOVED) if i in indexes and "inlineData" in p else p
                    for i, p in enumerate(stale["parts"])
                ],
            }
            contents = [masked if content is stale else content for content in contents]
        thinking: dict = {"includeThoughts": True}
        if self._thinking_level:
            thinking["thinkingLevel"] = self._thinking_level
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"thinkingConfig": thinking},
        }
        if tools:
            body["tools"] = tools
        if self.on_request is not None:
            self.on_request(body)
        parts: list[dict] = []
        last_chunk: dict = {}
        for chunk in self._transport(self._model, body):
            last_chunk = chunk
            content = _model_content(chunk) or {}
            for part in content.get("parts") or []:
                parts.append(part)
                if on_speech and part.get("text") and not part.get("thought"):
                    on_speech(part["text"])
        if not parts:
            # A 200 stream with no content at all: a safety block, a malformed
            # function call, or an empty candidate. Committing it would record
            # a silent, answerless exchange — raise instead, so the turn's
            # retry path keeps the events queued and the failure is visible.
            raise RuntimeError(f"gemini returned no content: {_empty_stream_reason(last_chunk)}")
        return {"candidates": [{"content": {"role": "model", "parts": _merged(parts)}}]}

    def absorb(self, user_message: dict, response: dict, *, latest_only_images: list[int] | None = None) -> Decision:
        """Commit the exchange to history and distill the model's Decision.

        Thought-summary parts are dropped from the stored model turn (they are
        display-only); everything else — including any thoughtSignature the
        model attached to its parts — is kept verbatim for the next request.

        ``latest_only_images`` names positions in this message's image list
        (order given to :meth:`user_message`) that must only ever appear in the
        newest turn — the wrist camera: a stale gripper close-up reads as
        current grasp state and misleads the model, so absorbing a new one
        prunes the previous turn's copy on the spot.
        """
        decision = _decision_from(response)
        self._history.append(user_message)
        if latest_only_images:
            if self._latest_only_turn is not None:
                content, indexes = self._latest_only_turn
                content["parts"] = [
                    dict(_WRIST_FRAME_REMOVED) if i in indexes and "inlineData" in p else p
                    for i, p in enumerate(content["parts"])
                ]
            image_parts = [i for i, p in enumerate(user_message["parts"]) if "inlineData" in p]
            self._latest_only_turn = (
                user_message,
                [image_parts[i] for i in latest_only_images if i < len(image_parts)],
            )
        model_content = _model_content(response)
        if model_content is not None:
            kept = [p for p in model_content.get("parts") or [] if not p.get("thought")]
            self._history.append({**model_content, "parts": kept or [{"text": ""}]})
        self._prune()
        return decision

    def add_tool_outcomes(self, outcomes: list[tuple[ToolCall, str]]) -> None:
        """Answer the model's function calls (the API requires one response per call)."""
        if not outcomes:
            return
        parts = []
        for call, outcome in outcomes:
            function_response = {"name": call.name, "response": {"outcome": outcome}}
            if call.id:
                function_response["id"] = call.id
            parts.append({"functionResponse": function_response})
        self._history.append({"role": "user", "parts": parts})

    def _prune(self) -> None:
        history = self._history
        while len(history) > self._max_history:
            history.pop(0)
        # The history must start with a plain user turn: a leading model turn or
        # an orphaned function response (whose call was just pruned) is rejected.
        while history and (
            history[0].get("role") != "user" or any("functionResponse" in p for p in history[0].get("parts") or [])
        ):
            history.pop(0)
        # A pruned turn must not stay pinned as the latest-only holder: absorb
        # would "prune" an orphan dict nothing reads, and the reference would
        # keep its base64 wrist frame alive for as long as the arm feed is stale.
        if self._latest_only_turn is not None and not any(c is self._latest_only_turn[0] for c in history):
            self._latest_only_turn = None
        # Keep camera frames only in the newest few user turns (none at all if
        # the configured keep-count is zero or nonsensical).
        keep = max(self._max_image_turns, 0)
        image_turns = [
            c for c in history if c.get("role") == "user" and any("inlineData" in p for p in c.get("parts") or [])
        ]
        for content in image_turns[:-keep] if keep else image_turns:
            content["parts"] = [dict(_FRAME_REMOVED) if "inlineData" in p else p for p in content["parts"]]


def _merged(parts: list[dict]) -> list[dict]:
    """Collapse adjacent plain-text deltas; anything carrying more than text
    (thoughts, signatures, calls) is kept verbatim for the stored history."""
    merged: list[dict] = []
    for part in parts:
        previous = merged[-1] if merged else None
        if previous is not None and set(previous) == {"text"} and set(part) == {"text"}:
            previous["text"] += part["text"]
        else:
            merged.append(dict(part))
    return merged


def _model_content(response: dict) -> dict | None:
    candidates = response.get("candidates") or []
    return candidates[0].get("content") if candidates else None


def _empty_stream_reason(last_chunk: dict) -> str:
    """Why a stream carried no parts, from whatever the API did say."""
    candidates = last_chunk.get("candidates") or [{}]
    reason = {
        "finishReason": candidates[0].get("finishReason"),
        "blockReason": (last_chunk.get("promptFeedback") or {}).get("blockReason"),
    }
    details = ", ".join(f"{k}={v}" for k, v in reason.items() if v)
    return details or "empty stream"


def _decision_from(response: dict) -> Decision:
    decision = Decision()
    content = _model_content(response) or {}
    for part in content.get("parts") or []:
        call = part.get("functionCall")
        if call is not None:
            args = call.get("args") or {}
            decision.calls.append(
                ToolCall(
                    name=call.get("name") or "", args=args if isinstance(args, dict) else {}, id=call.get("id") or ""
                )
            )
        elif part.get("text"):
            if part.get("thought"):
                decision.thoughts = f"{decision.thoughts or ''}{part['text']}".strip()
            else:
                decision.speech = f"{decision.speech or ''}{part['text']}".strip()
    decision.speech = _clean_speech(decision.speech)
    return decision


_TOOL_NARRATION = re.compile(r"Calling tool\b")


def split_tool_narration(text: str) -> tuple[str, bool]:
    """Cut leaked tool-call narration ("Calling tool ..." to end of text).

    gemini-3 preview sometimes appends it to its reply, without a sentence
    boundary. Returns ``(clean text, whether narration was found)`` — the one
    scrub both the chat transcript (:func:`_clean_speech`) and the audio path
    (``SpeechStreamer._say``) apply, so the two can never diverge.
    """
    match = _TOOL_NARRATION.search(text)
    if match is None:
        return text, False
    return text[: match.start()].rstrip(), True


def _clean_speech(speech: str | None) -> str | None:
    """Drop unspeakable output: placeholders and leaked tool-call narration."""
    if not speech:
        return None
    speech, _ = split_tool_narration(speech)
    return speech if re.search(r"[a-zA-Z0-9]", speech) else None
