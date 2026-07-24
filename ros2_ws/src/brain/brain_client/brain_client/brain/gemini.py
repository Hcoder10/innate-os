# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Gemini inference for the local brain, over the native Gemini REST API.

The brain reaches Gemini through the Innate proxy (service ``gemini`` — the
proxy holds the upstream key and passes native ``:generateContent`` calls
through untouched; the robot authenticates with its service key) or directly
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
call and only *reads* the history, so the agent runs it on a worker thread.
All history mutation (:meth:`absorb`, :meth:`add_tool_outcomes`, :meth:`clear`)
happens on the ROS executor thread.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

import httpx

STOP_SKILL = "stop_current_skill"
WAIT = "wait"
GO_TO_POINT = "go_to_point"

# An explicit no-op keeps idle turns clean: without it, models tend to emit
# placeholder text ("[]", "Empty response") rather than returning nothing.
_WAIT_DECLARATION = {
    "name": WAIT,
    "description": "Do nothing until the next update. Use when there is nothing new to do or say.",
}

# Visual navigation grounding: the model points at a floor pixel and the robot
# projects it into a local navigation goal (brain/grounding.py). Declared only
# when navigate_to_position is among the active skills — it is the actuator.
_GO_TO_POINT_DECLARATION = {
    "name": GO_TO_POINT,
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
GENERATE_PATH = "/v1beta/models/{model}:generateContent"

_FRAME_REMOVED = {"text": "[older camera frame removed]"}

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
    """Skill name -> valid function name."""
    return re.sub(r"[^a-zA-Z0-9_.\-]", "_", skill_name)[:64] or "skill"


def assign_tool_names(skills: list[dict]) -> list[tuple[str, dict]]:
    """Give every skill a unique function name, in roster order.

    Sanitizing/truncating can make two skill names collide (and a skill can
    shadow a built-in tool); colliding names get a numeric suffix so a call
    never silently dispatches to the wrong skill.
    """
    taken = {STOP_SKILL, WAIT, GO_TO_POINT}
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


def build_tools(skills: list[dict], running_skill_name: str | None, *, can_go_to_point: bool = False) -> list[dict]:
    """One function declaration per available skill, in a native tools block.

    While a skill runs, the robot's only action is stopping it, so the
    declarations collapse to stop_current_skill.
    """
    if running_skill_name is not None:
        stop = {
            "name": STOP_SKILL,
            "description": (
                f"Abort the currently running skill ({running_skill_name}). Use when it is clearly "
                "failing, no longer makes sense, or the user asks for something else."
            ),
        }
        return [{"functionDeclarations": [stop, _WAIT_DECLARATION]}]
    declarations = [_declaration(name, meta) for name, meta in assign_tool_names(skills)]
    if can_go_to_point:
        declarations.append(_GO_TO_POINT_DECLARATION)
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


# ---------- transports (callable: (model, body) -> response dict) ----------


def proxy_transport(proxy):
    """Reach Gemini through the Innate proxy (the proxy holds the upstream key)."""

    def send(model: str, body: dict) -> dict:
        endpoint = GENERATE_PATH.format(model=model)
        with proxy.request_stream(PROXY_SERVICE, endpoint, json=body) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise RuntimeError(f"gemini via proxy: HTTP {resp.status_code}: {payload[:200]!r}")
            return json.loads(payload)

    return send


def direct_transport(api_key: str):
    """Reach Google's Gemini API directly with GEMINI_API_KEY."""

    def send(model: str, body: dict) -> dict:
        url = DIRECT_BASE_URL + GENERATE_PATH.format(model=model)
        resp = httpx.post(url, headers={"x-goog-api-key": api_key}, json=body, timeout=90.0)
        if resp.status_code != 200:
            raise RuntimeError(f"gemini direct: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    return send


class GeminiSession:
    """A bounded conversation with Gemini: one generate() per agent turn."""

    def __init__(self, transport, *, model: str, thinking_level: str, max_history: int, max_image_turns: int):
        self._transport = transport
        self._model = model
        self._thinking_level = thinking_level
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        self._history: list[dict] = []
        # Bumped by clear(); the agent discards turns started under an older generation.
        self.generation = 0

    def clear(self) -> None:
        self._history = []
        self.generation += 1

    @staticmethod
    def user_message(text: str, images: list[bytes]) -> dict:
        parts = [{"text": text}]
        for jpeg in images:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
        return {"role": "user", "parts": parts}

    def generate(self, user_message: dict, tools: list[dict], system: str) -> dict:
        """Blocking network call — safe on a worker thread (history is only read)."""
        thinking = {"includeThoughts": True}
        if self._thinking_level:
            thinking["thinkingLevel"] = self._thinking_level
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [*self._history, user_message],
            "generationConfig": {"thinkingConfig": thinking},
        }
        if tools:
            body["tools"] = tools
        return self._transport(self._model, body)

    def absorb(self, user_message: dict, response: dict) -> Decision:
        """Commit the exchange to history and distill the model's Decision.

        Thought-summary parts are dropped from the stored model turn (they are
        display-only); everything else — including any thoughtSignature the
        model attached to its parts — is kept verbatim for the next request.
        """
        decision = _decision_from(response)
        self._history.append(user_message)
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
        # Keep camera frames only in the newest few user turns (none at all if
        # the configured keep-count is zero or nonsensical).
        keep = max(self._max_image_turns, 0)
        image_turns = [
            c for c in history if c.get("role") == "user" and any("inlineData" in p for p in c.get("parts") or [])
        ]
        for content in image_turns[:-keep] if keep else image_turns:
            content["parts"] = [dict(_FRAME_REMOVED) if "inlineData" in p else p for p in content["parts"]]


def _model_content(response: dict) -> dict | None:
    candidates = response.get("candidates") or []
    return candidates[0].get("content") if candidates else None


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
    return decision
