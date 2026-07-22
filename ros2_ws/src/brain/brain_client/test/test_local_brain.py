# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the local brain's pure core (no ROS, no network).

Covers the Gemini layer the agent loop depends on: skill metadata -> tool
declarations, response -> Decision (speech / thoughts / calls), and the history
pruning that keeps requests small (the "image cache"). GeminiSession only
touches the network in generate(), so everything else is exercised directly
with a None or capturing transport.
"""

import json

import pytest

from brain_client.brain.gemini import (
    STOP_SKILL,
    GeminiSession,
    _decision_from,
    build_tools,
    tool_name,
)
from brain_client.brain.prompt import build_system_prompt

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"

NAV_SKILL = {
    "id": "innate-os/navigate_to_position",
    "name": "navigate_to_position",
    "guidelines": "Navigate to x, y (meters).",
    "inputs": {
        "x": {"type": "float", "required": True},
        "y": {"type": "float", "required": True},
        "local_frame": {"type": "bool", "required": False, "default": False},
        "mode": {"type": "str", "required": False, "enum": ["fast", "safe"]},
    },
}
WAVE_SKILL = {"id": "local/wave", "name": "wave", "guidelines": "Wave the arm.", "inputs": {}}


def make_session(transport=None, max_history=60, max_image_turns=2) -> GeminiSession:
    return GeminiSession(
        transport, model="test-model", thinking_level="", max_history=max_history, max_image_turns=max_image_turns
    )


def model_response(*parts) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}}]}


def call_part(name: str, args: dict, call_id: str = "") -> dict:
    call = {"name": name, "args": args}
    if call_id:
        call["id"] = call_id
    return {"functionCall": call}


# ---------- tool building ----------


def test_tool_name_sanitizes_invalid_characters():
    assert tool_name("navigate_to_position") == "navigate_to_position"
    assert tool_name("Wave Hello!") == "Wave_Hello_"
    assert len(tool_name("x" * 100)) == 64


def test_build_tools_declares_one_function_per_skill_plus_wait():
    tools = build_tools([NAV_SKILL, WAVE_SKILL], None)
    declarations = tools[0]["functionDeclarations"]
    assert [d["name"] for d in declarations] == ["navigate_to_position", "wave", "wait"]

    nav = declarations[0]
    assert nav["description"] == NAV_SKILL["guidelines"]
    params = nav["parameters"]
    assert params["type"] == "OBJECT"
    assert set(params["properties"]) == {"x", "y", "local_frame", "mode"}
    assert params["required"] == ["x", "y"]
    assert params["properties"]["x"]["type"] == "NUMBER"
    assert params["properties"]["local_frame"]["type"] == "BOOLEAN"
    assert params["properties"]["mode"]["enum"] == ["fast", "safe"]
    # No-input skills must not carry an empty object schema.
    assert "parameters" not in declarations[1]


def test_build_tools_while_running_offers_only_stop_and_wait():
    declarations = build_tools([NAV_SKILL, WAVE_SKILL], "navigate_to_position")[0]["functionDeclarations"]
    assert [d["name"] for d in declarations] == [STOP_SKILL, "wait"]
    assert "navigate_to_position" in declarations[0]["description"]


def test_build_tools_with_no_skills_still_offers_wait():
    declarations = build_tools([], None)[0]["functionDeclarations"]
    assert [d["name"] for d in declarations] == ["wait"]


def test_unknown_param_type_falls_back_to_annotated_string():
    skill = {"id": "s", "name": "s", "guidelines": "g", "inputs": {"blob": {"type": "list[str]", "required": True}}}
    schema = build_tools([skill], None)[0]["functionDeclarations"][0]["parameters"]["properties"]["blob"]
    assert schema["type"] == "STRING"
    assert "list[str]" in schema["description"]


# ---------- decisions ----------


def test_decision_separates_speech_thoughts_and_calls():
    response = model_response(
        {"text": "I see a person.", "thought": True},
        {"text": "Hello there!"},
        call_part("wave", {}, "c1"),
    )
    decision = _decision_from(response)
    assert decision.speech == "Hello there!"
    assert decision.thoughts == "I see a person."
    assert [(c.id, c.name, c.args) for c in decision.calls] == [("c1", "wave", {})]


def test_decision_tolerates_empty_or_malformed_response():
    assert _decision_from({"candidates": []}).calls == []
    assert _decision_from({}).speech is None
    decision = _decision_from(model_response({"functionCall": {"name": "wave", "args": None}}))
    assert decision.calls[0].args == {}


# ---------- history / image pruning ----------


def user_turn(text: str, with_image: bool) -> dict:
    return GeminiSession.user_message(text, [JPEG] if with_image else [])


def images_in(content: dict) -> int:
    return sum(1 for p in content.get("parts") or [] if "inlineData" in p)


def test_prune_keeps_images_only_in_newest_turns():
    session = make_session(max_image_turns=2)
    for i in range(5):
        session.absorb(user_turn(f"turn {i}", with_image=True), model_response({"text": "ok"}))

    user_turns = [c for c in session._history if c["role"] == "user"]
    assert [images_in(c) for c in user_turns] == [0, 0, 0, 1, 1]
    # Stripped frames leave a placeholder so the transcript still reads coherently.
    assert any("removed" in p.get("text", "") for p in user_turns[0]["parts"])


def test_prune_caps_history_and_never_starts_on_orphaned_function_response():
    session = make_session(max_history=4)
    for i in range(6):
        decision = session.absorb(user_turn(f"turn {i}", False), model_response(call_part("wave", {}, f"c{i}")))
        session.add_tool_outcomes([(decision.calls[0], "started")])

    history = session._history
    assert len(history) <= 4
    assert history[0]["role"] == "user"
    assert not any("functionResponse" in p for p in history[0]["parts"])


def test_absorb_drops_thought_parts_from_stored_history():
    session = make_session()
    session.absorb(
        user_turn("hi", False),
        model_response({"text": "planning...", "thought": True}, {"text": "Hello!"}),
    )
    model_turn = session._history[-1]
    assert model_turn["role"] == "model"
    assert [p.get("text") for p in model_turn["parts"]] == ["Hello!"]


def test_clear_bumps_generation_for_stale_turn_detection():
    session = make_session()
    generation = session.generation
    session.absorb(user_turn("hi", False), model_response({"text": "hello"}))
    session.clear()
    assert session.generation == generation + 1
    assert session._history == []


def test_tool_outcomes_are_recorded_as_function_responses():
    session = make_session()
    decision = session.absorb(user_turn("go", False), model_response(call_part("navigate_to_position", {"x": 1}, "abc")))
    session.add_tool_outcomes([(decision.calls[0], "started")])
    part = session._history[-1]["parts"][0]["functionResponse"]
    assert part["name"] == "navigate_to_position"
    assert part["id"] == "abc"
    assert part["response"] == {"outcome": "started"}


def test_generate_builds_a_complete_native_request():
    captured = {}

    def transport(model, body):
        captured["model"] = model
        captured.update(body)
        return model_response({"text": "ok"})

    session = make_session(transport=transport)
    tools = build_tools([WAVE_SKILL], None)
    session.generate(user_turn("hello", True), tools, "SYSTEM")
    assert captured["model"] == "test-model"
    assert captured["systemInstruction"] == {"parts": [{"text": "SYSTEM"}]}
    assert captured["tools"] == tools
    assert captured["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}
    assert images_in(captured["contents"][-1]) == 1


def test_generate_sets_thinking_level_when_configured():
    captured = {}

    def transport(model, body):
        captured.update(body)
        return model_response({"text": "ok"})

    session = GeminiSession(transport, model="m", thinking_level="high", max_history=10, max_image_turns=2)
    session.generate(user_turn("hi", False), [], "S")
    assert captured["generationConfig"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "high"}


# ---------- prompt ----------


def test_system_prompt_embeds_directive_and_defaults_when_empty():
    assert "Patrol the house" in build_system_prompt("Patrol the house")
    assert "helpful home robot" in build_system_prompt(None)
    assert "helpful home robot" in build_system_prompt("   ")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
