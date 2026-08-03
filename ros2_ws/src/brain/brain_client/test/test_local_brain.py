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
    WAIT,
    GeminiSession,
    _decision_from,
    assign_tool_names,
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


def test_assign_tool_names_disambiguates_collisions_and_builtins():
    colliding = [
        {"id": "a", "name": "Wave Hello!"},
        {"id": "b", "name": "Wave Hello?"},  # sanitizes to the same name as 'a'
        {"id": "c", "name": "wait"},  # shadows the built-in wait tool
        {"id": "d", "name": "y" * 100},
        {"id": "e", "name": "y" * 100},  # truncates to the same name as 'd'
    ]
    named = assign_tool_names(colliding)
    names = [name for name, _ in named]
    assert names[0] == "Wave_Hello_"
    assert names[1] == "Wave_Hello__2"
    assert names[2] == "wait_2"
    assert len(names) == len(set(names)) and WAIT not in names
    assert all(len(name) <= 64 for name in names)
    # The declarations use the same disambiguated names.
    declared = [d["name"] for d in build_tools(colliding, None)[0]["functionDeclarations"]]
    assert declared[:5] == names


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


def test_prune_with_zero_image_turns_strips_every_frame():
    session = make_session(max_image_turns=0)
    for i in range(3):
        session.absorb(user_turn(f"turn {i}", with_image=True), model_response({"text": "ok"}))
    user_turns = [c for c in session._history if c["role"] == "user"]
    assert all(images_in(c) == 0 for c in user_turns)


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


def test_clear_empties_history():
    session = make_session()
    session.absorb(user_turn("hi", False), model_response({"text": "hello"}))
    session.clear()
    assert session._history == []


def test_tool_outcomes_are_recorded_as_function_responses():
    session = make_session()
    decision = session.absorb(
        user_turn("go", False), model_response(call_part("navigate_to_position", {"x": 1}, "abc"))
    )
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


# ---------- visual grounding (pixel -> floor target) ----------

from brain_client.brain import grounding  # noqa: E402


def fake_jpeg(width: int, height: int) -> bytes:
    """Minimal JPEG header: SOI + SOF0 carrying the given dimensions."""
    return b"\xff\xd8" + b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x00" * 12


CAM = dict(vertical_fov_deg=80.0, cam_height=0.19663, cam_forward=0.0197)
FRAME = fake_jpeg(640, 480)


def test_jpeg_dimensions_reads_sof():
    assert grounding.jpeg_dimensions(fake_jpeg(1280, 800)) == (1280, 800)
    assert grounding.jpeg_dimensions(b"not a jpeg") is None


def test_center_pixel_with_head_down_projects_ahead():
    # Head 10° down, image center: floor hit at cam_forward + h/tan(10°) along +x.
    x, y = grounding.pixel_to_floor(500, 500, frame_jpeg=FRAME, pitch_deg=-10.0, **CAM)
    assert abs(x - 1.135) < 0.01
    assert abs(y) < 1e-6


def test_center_pixel_level_camera_is_horizon():
    assert grounding.pixel_to_floor(500, 500, frame_jpeg=FRAME, pitch_deg=0.0, **CAM) is None
    # Above the horizon even more so.
    assert grounding.pixel_to_floor(500, 100, frame_jpeg=FRAME, pitch_deg=0.0, **CAM) is None


def test_bottom_edge_is_close_and_left_is_positive_y():
    x, y = grounding.pixel_to_floor(500, 1000, frame_jpeg=FRAME, pitch_deg=0.0, **CAM)
    assert abs(x - 0.254) < 0.01
    left_x, left_y = grounding.pixel_to_floor(250, 900, frame_jpeg=FRAME, pitch_deg=0.0, **CAM)
    assert left_y > 0  # left half of the image -> +y (robot's left)


def test_near_horizon_pixel_is_range_capped():
    x, y = grounding.pixel_to_floor(500, 510, frame_jpeg=FRAME, pitch_deg=0.0, **CAM)
    import math

    assert abs(math.hypot(x, y) - grounding.MAX_RANGE_M) < 1e-6


def test_approach_goal_stops_short_and_faces_the_point():
    goal = grounding.approach_goal(2.0, 0.0)
    assert abs(goal["x"] - (2.0 - grounding.STANDOFF_M)) < 1e-6
    assert goal["y"] == 0.0
    assert goal["theta_degrees"] == 0.0
    assert goal["local_frame"] is True
    # Point already inside the standoff: no travel, just turn to face it.
    close = grounding.approach_goal(0.0, 0.2)
    assert close["x"] == 0.0 and close["y"] == 0.0
    assert abs(close["theta_degrees"] - 90.0) < 1e-6


# ---------- agent loop (fake node, no network) ----------

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from brain_client.brain.agent import BrainAgent  # noqa: E402
from brain_client.core.state import BrainState  # noqa: E402


@pytest.fixture
def agent_factory(monkeypatch):
    """Build agents against stub collaborators; shut their loop threads down after."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")  # gives the agent a swappable transport
    created = []

    def make(trace=None) -> tuple[BrainAgent, BrainState]:
        logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
        node = SimpleNamespace(get_logger=lambda: logger)
        config = SimpleNamespace(
            gemini_model="m",
            gemini_thinking_level="",
            history_max_entries=60,
            history_max_image_turns=2,
            idle_turn_interval=3.0,
            supervision_turn_interval=5.0,
        )
        state = BrainState()
        state.is_brain_active = True
        camera = SimpleNamespace(
            fresh_image_jpeg=lambda max_age: JPEG, fresh_arm_jpeg=lambda max_age: None, current_head_pitch=-10.0
        )
        pose = SimpleNamespace(current_pose_xyt=lambda: None, is_mapfree=False)
        chat = SimpleNamespace(emit_system=lambda *a, **k: None, emit=lambda *a, **k: None, speak=lambda *a, **k: None)
        agent = BrainAgent(
            node,
            state,
            config,
            camera=camera,
            pose_tracker=pose,
            runner=SimpleNamespace(),
            roster=SimpleNamespace(active_skill_ids=lambda: []),
            chat=chat,
            gaze=SimpleNamespace(pause=lambda: None),
            trace=trace,
        )
        created.append(agent)
        return agent, state

    yield make
    for agent in created:
        agent.shutdown()


def run_turn(agent: BrainAgent) -> None:
    """Run one turn to completion on the agent's own loop thread."""
    asyncio.run_coroutine_threadsafe(agent._turn(), agent._loop).result(timeout=5)


def no_pause(agent: BrainAgent, monkeypatch) -> None:
    """Skip the between-turns / backoff pauses so tests don't sleep."""

    async def skip(seconds, *, seen=0):
        pass

    monkeypatch.setattr(agent, "_pause", skip)


def test_failed_turn_leaves_events_queued_for_the_retry(agent_factory, monkeypatch):
    agent, state = agent_factory()

    def transport(model, body):
        raise RuntimeError("boom")

    agent._session._transport = transport
    no_pause(agent, monkeypatch)
    agent.on_user_message("bring me a snack")
    run_turn(agent)

    # Nothing was consumed (turns are transactional): the retry re-sends them.
    assert [e["text"] for e in agent._events] == ['The user says: "bring me a snack"']
    assert agent._session._history == []  # the failed exchange never entered history
    assert agent._error_streak == 1


def test_committed_turn_consumes_exactly_the_events_it_saw(agent_factory):
    agent, state = agent_factory()
    agent._session._transport = lambda model, body: model_response(call_part("wait", {}))
    agent.on_user_message("hello")
    run_turn(agent)

    assert agent._events == []
    # History: the user turn, the model turn, and the wait call's functionResponse.
    assert agent._session.history_len == 3


def test_turn_finishing_after_deactivation_is_dropped_entirely(agent_factory):
    agent, state = agent_factory()

    def transport(model, body):  # deactivation lands while the turn is thinking
        state.is_brain_active = False
        return model_response({"text": "stale"})

    agent._session._transport = transport
    run_turn(agent)

    assert agent._session._history == []  # no stale observation survives into the next activation
    assert agent._turn_in_flight is False


def test_stop_cancels_a_turn_mid_think_and_absorbs_nothing(agent_factory):
    agent, state = agent_factory()
    thinking, release = threading.Event(), threading.Event()

    def transport(model, body):
        thinking.set()
        release.wait(timeout=5)
        return model_response({"text": "stale"})

    agent._session._transport = transport
    agent.on_user_message("hi")
    agent.start()
    assert thinking.wait(timeout=5)

    agent.stop()  # synchronous: the turn has unwound at its await when this returns
    release.set()  # the orphaned HTTP call finishes on its worker thread...
    time.sleep(0.2)
    assert agent._session._history == []  # ...and its response is dropped
    assert agent._task is None


def test_reset_mid_turn_restarts_the_loop_with_empty_history(agent_factory):
    agent, state = agent_factory()
    thinking, release = threading.Event(), threading.Event()

    def transport(model, body):
        thinking.set()
        release.wait(timeout=5)
        return model_response({"text": "stale"})

    agent._session._transport = transport
    agent.start()
    assert thinking.wait(timeout=5)
    thinking.clear()

    agent.reset()  # cancels the old turn, clears history, respawns the loop
    assert agent._task is not None
    assert agent._session._history == []
    assert thinking.wait(timeout=5)  # the restarted loop is already thinking again
    agent.stop()
    release.set()


def test_trace_reports_the_turn_lifecycle(agent_factory, monkeypatch):
    traces = []
    agent, state = agent_factory(trace=lambda payload: traces.append(json.loads(payload)))
    no_pause(agent, monkeypatch)

    outcomes = iter([RuntimeError("boom"), model_response(call_part("wait", {}))])

    def transport(model, body):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    agent._session._transport = transport
    agent.on_user_message("hello")
    run_turn(agent)  # fails...
    run_turn(agent)  # ...retries the same still-queued event and commits
    state.is_brain_active = False
    agent._snapshot()  # the telemetry heartbeat reports even while inactive

    events = [t["ev"] for t in traces]
    assert events == ["event", "turn_start", "turn_error", "turn_start", "turn_end", "snapshot"]
    assert traces[0]["kind"] == "user"
    assert traces[2]["streak"] == 1 and traces[2]["backoff"] == 5.0
    assert traces[4]["calls"] == [{"name": "wait", "args": {}, "outcome": "ok"}]
    snapshot = traces[5]
    # History: the user turn, the model turn, and the wait call's functionResponse.
    assert snapshot["active"] is False and snapshot["backend"] == "gemini-direct" and snapshot["history"] == 3


# ---------- prompt ----------


def test_system_prompt_embeds_directive_and_defaults_when_empty():
    assert "Patrol the house" in build_system_prompt("Patrol the house")
    assert "helpful home robot" in build_system_prompt(None)
    assert "helpful home robot" in build_system_prompt("   ")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
