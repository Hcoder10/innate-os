# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Household resident dialogue stays private, grounded, and run-scoped.

These tests exercise the real challenge sidecar and judge with a tiny fake
world. MuJoCo and ROS are deliberately absent: dialogue consumes the same
ground-truth snapshot ``tick`` already receives, and the rosbridge decoder is
pure JSON.
"""

import json
import math
import socket
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PACKAGE = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"
CHALLENGES = REPO_ROOT / "sim" / "challenges"

# challenges.py imports world.py for path defaults. Only two MuJoCo names are
# evaluated as annotations while world.py imports; the fake world below never
# calls any model function. Keep this test runnable in the fast, no-sim suite.
try:
    import mujoco  # noqa: F401
except ImportError:
    fake_mujoco = types.ModuleType("mujoco")
    fake_mujoco.MjModel = object
    fake_mujoco.MjSpec = object
    sys.modules["mujoco"] = fake_mujoco

sys.path.insert(0, str(DRIVER_PACKAGE))

from mars_sim_driver.challenges import (  # noqa: E402
    AllOf,
    Challenge,
    ChallengeChatBridge,
    ChallengeEngine,
    EventSeen,
    Goal,
    load_challenges,
)
from mars_sim_driver.props import Prop, PropRegistry, load_props  # noqa: E402


class FakeSim:
    """Only the scene operations ChallengeEngine.start() needs."""

    def __init__(self) -> None:
        self.data = SimpleNamespace(time=0.0)
        self.dropped: dict[str, tuple[float, float, float]] = {}

    def reset(self) -> None:
        self.data.time = 0.0
        self.dropped.clear()

    def drop_prop_at(self, name: str, x: float, y: float, yaw: float) -> bool:
        self.dropped[name] = (x, y, yaw)
        return True


def _source_challenge() -> Challenge:
    return load_challenges([CHALLENGES])["household_orders"]


def test_loader_discovers_file_and_package_challenges():
    loaded = load_challenges([CHALLENGES])

    assert {"victory_lap", "lifesupport", "rescue", "shepherd", "household_orders"} <= loaded.keys()
    assert loaded["household_orders"].runtime is not None


def _engine(
    tmp_path: Path,
    resident_ids: tuple[str, ...] = ("alex", "blake", "casey"),
    *,
    source_goals: bool = False,
):
    source = _source_challenge()
    assert source.runtime is not None
    residents = [resident for resident in source.runtime.residents if resident.id in resident_ids]
    props = {resident.prop for resident in residents}
    runtime = type(source.runtime)(residents)
    challenge = Challenge(
        id=source.id,
        title=source.title,
        brief=source.brief,
        setup=[drop for drop in source.setup if drop.name in props],
        goals=(
            source.goals
            if source_goals
            else [
                Goal(
                    "Collect every order",
                    AllOf(
                        [
                            EventSeen("resident_order_confirmed", {"resident": resident_id})
                            for resident_id in resident_ids
                        ]
                    ),
                )
            ]
        ),
        runtime=runtime,
        time_limit_s=source.time_limit_s,
    )
    sim = FakeSim()
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=tmp_path / "progress.json")
    engine.challenges = {challenge.id: challenge}
    assert engine.start(challenge.id)
    centers = {drop.name: (drop.x, drop.y) for drop in challenge.setup}
    by_id = {resident.id: resident for resident in residents}
    return engine, sim, centers, by_id


def _tick(engine: ChallengeEngine, sim: FakeSim, centers: dict[str, tuple[float, float]], robot_pose):
    sim.data.time += 0.1
    if len(robot_pose) == 2:
        robot_pose = (*robot_pose, 0.0)
    return engine.tick(sim.data.time, robot_pose, centers, engine.world_epoch)


def _speak(engine: ChallengeEngine, sim: FakeSim, centers, robot_pose, text: str):
    engine.post_robot_speech(text, timestamp=123.0)
    return _tick(engine, sim, centers, robot_pose)


def _reply(engine: ChallengeEngine) -> tuple[int, dict]:
    item = engine.next_chat_input(timeout=0.0)
    assert item is not None
    return item


def _robot_frame(sender: str, text: str, topic: str = ChallengeChatBridge.CHAT_OUT) -> str:
    return json.dumps(
        {
            "topic": topic,
            "msg": {"data": json.dumps({"sender": sender, "text": text, "timestamp": 42.5})},
        }
    )


def test_bridge_accepts_only_visible_robot_speech():
    assert ChallengeChatBridge.robot_speech(_robot_frame("robot", "Hello")) == ("Hello", 42.5)

    for sender in ("robot_thoughts", "robot_anticipation", "system", "skill_output", "user"):
        assert ChallengeChatBridge.robot_speech(_robot_frame(sender, "not spoken")) is None
    assert ChallengeChatBridge.robot_speech(_robot_frame("robot", "Hello", topic="/somewhere/else")) is None
    assert ChallengeChatBridge.robot_speech(_robot_frame("robot", "   ")) is None


def test_far_speech_is_silent(tmp_path):
    engine, sim, centers, _residents = _engine(tmp_path)

    block = _speak(engine, sim, centers, (100.0, 100.0), "Does anyone want dinner?")

    assert engine.next_chat_input(timeout=0.0) is None
    assert block["active"]["state"] == "running"
    assert not block["active"]["goals"][0]["done"]


def test_first_nearby_speech_reveals_the_private_order(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path)
    alex = residents["alex"]

    block = _speak(engine, sim, centers, centers[alex.prop], "Hi, what would you like from DoorDash?")
    _token, payload = _reply(engine)

    assert alex.order in payload["text"]
    assert payload["sender"] == "user"
    assert "origin" not in payload  # do not reveal challenge metadata to the robot stack
    assert block["active"]["state"] == "running"
    assert not block["active"]["goals"][0]["done"]


def test_household_resident_replies_and_confirms_at_two_metre_boundary(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    x, y = centers[alex.prop]

    block = _speak(engine, sim, centers, (x - 2.01, y), "What would you like?")
    assert engine.next_chat_input(timeout=0.0) is None
    assert block["active"]["state"] == "running"

    block = _speak(engine, sim, centers, (x - 2.0, y), "What would you like?")
    assert alex.order in _reply(engine)[1]["text"]
    assert block["active"]["state"] == "running"

    block = _speak(engine, sim, centers, (x - 2.0, y), alex.order)
    assert "That's correct" in _reply(engine)[1]["text"]
    assert block["active"]["state"] == "passed"


def test_resident_behind_robot_stays_silent_then_replies_when_faced(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    x, y = centers[alex.prop]
    robot_x = x - 1.79

    block = _speak(engine, sim, centers, (robot_x, y, math.pi), "What would you like?")
    assert engine.next_chat_input(timeout=0.0) is None
    assert block["active"]["state"] == "running"

    block = _speak(engine, sim, centers, (robot_x, y, 0.0), "What would you like?")
    assert alex.order in _reply(engine)[1]["text"]
    assert block["active"]["state"] == "running"


@pytest.mark.parametrize(("bearing_degrees", "should_reply"), [(49.0, True), (51.0, False)])
def test_resident_reply_tracks_head_camera_field_of_view(tmp_path, bearing_degrees, should_reply):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    bearing = math.radians(bearing_degrees)
    centers = {alex.prop: (1.5 * math.cos(bearing), 1.5 * math.sin(bearing))}

    block = _speak(engine, sim, centers, (0.0, 0.0, 0.0), "What would you like?")
    reply = engine.next_chat_input(timeout=0.0)

    assert (reply is not None) is should_reply
    if reply is not None:
        assert alex.order in reply[1]["text"]
    assert block["active"]["state"] == "running"


def test_nearest_resident_in_front_is_selected(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path)
    centers = {
        residents["alex"].prop: (-0.5, 0.0),  # closest, but behind
        residents["blake"].prop: (1.5, 0.0),
        residents["casey"].prop: (1.0, 0.25),  # closest visible resident
    }

    block = _speak(engine, sim, centers, (0.0, 0.0, 0.0), "What would you like?")
    _token, payload = _reply(engine)

    assert residents["casey"].order in payload["text"]
    assert residents["alex"].order not in payload["text"]
    assert residents["blake"].order not in payload["text"]
    assert block["active"]["state"] == "running"


def test_wrong_readback_corrects_without_progress(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    block = _speak(engine, sim, centers, position, "A chicken bowl from Chipotle with brown rice.")
    _token, payload = _reply(engine)

    assert "Not quite" in payload["text"]
    assert alex.order in payload["text"]
    assert block["active"]["state"] == "running"
    assert not block["active"]["goals"][0]["done"]


def test_contradictory_readback_does_not_confirm(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    contradiction = f"{alex.order} Put cheese on it too."
    block = _speak(engine, sim, centers, position, contradiction)
    _token, payload = _reply(engine)

    assert "Not quite" in payload["text"]
    assert block["active"]["state"] == "running"
    assert not block["active"]["goals"][0]["done"]


def test_connective_words_do_not_reject_a_correct_readback(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    block = _speak(engine, sim, centers, position, f"Actually, {alex.order}")
    _token, payload = _reply(engine)

    assert "That's correct" in payload["text"]
    assert block["active"]["state"] == "passed"


@pytest.mark.parametrize(
    "readback",
    [
        None,  # replaced with the resident's exact sentence below
        "Chipotle chicken bowl with brown rice, black beans, mild salsa, and without cheese.",
    ],
)
def test_complete_exact_or_paraphrased_readback_confirms(tmp_path, readback):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    block = _speak(engine, sim, centers, position, readback or alex.order)
    _token, payload = _reply(engine)

    assert "That's correct" in payload["text"]
    assert block["active"]["state"] == "passed"
    assert block["active"]["goals"][0]["done"]


def test_three_residents_can_complete_out_of_declaration_order(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path)

    for index, resident_id in enumerate(("casey", "alex", "blake")):
        resident = residents[resident_id]
        position = centers[resident.prop]
        _speak(engine, sim, centers, position, "What would you like?")
        assert resident.order in _reply(engine)[1]["text"]
        block = _speak(engine, sim, centers, position, resident.order)
        assert "That's correct" in _reply(engine)[1]["text"]
        assert block["active"]["state"] == ("passed" if index == 2 else "running")

    assert block["active"]["goals"][0]["done"]


def test_source_challenge_tracks_each_resident_then_doordash(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, source_goals=True)
    expected_goal = {"alex": 0, "blake": 1, "casey": 2}

    # A checkout completion before the resident phase is finished is not
    # deferred into the final goal.
    engine.post_event(
        {
            "status": "completed",
            "skill_id": "innate-os/place_doordash_order",
            "skill_name": "place_doordash_order",
        }
    )
    block = _tick(engine, sim, centers, (100.0, 100.0))
    assert [goal["done"] for goal in block["active"]["goals"]] == [False, False, False, False]

    for resident_id in ("casey", "alex", "blake"):
        resident = residents[resident_id]
        position = centers[resident.prop]
        _speak(engine, sim, centers, position, "What would you like?")
        _reply(engine)
        block = _speak(engine, sim, centers, position, resident.order)
        _reply(engine)
        assert block["active"]["goals"][expected_goal[resident_id]]["done"]
        assert not block["active"]["goals"][3]["done"]

    assert block["active"]["state"] == "running"
    assert [goal["done"] for goal in block["active"]["goals"]] == [True, True, True, False]

    engine.post_event(
        {
            "status": "completed",
            "skill_id": "innate-os/place_doordash_order",
            "skill_name": "place_doordash_order",
        }
    )
    block = _tick(engine, sim, centers, (100.0, 100.0))
    assert block["active"]["state"] == "passed"
    assert [goal["done"] for goal in block["active"]["goals"]] == [True, True, True, True]


def test_restart_clears_dialogue_and_confirmation_state(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    assert engine.start("household_orders")
    block = _speak(engine, sim, centers, position, alex.order)
    _token, payload = _reply(engine)

    # After a restart even a lucky exact guess is the first turn: the resident
    # reveals the order, but cannot confirm until a later readback.
    assert alex.order in payload["text"]
    assert "That's correct" not in payload["text"]
    assert block["active"]["state"] == "running"
    assert not block["active"]["goals"][0]["done"]


def test_stale_replies_are_dropped_after_restart_and_abort(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    position = centers[residents["alex"].prop]

    # Leave a reply queued under the old run, then replace that run.
    _speak(engine, sim, centers, position, "What is your order?")
    assert engine.start("household_orders")
    assert engine.next_chat_input(timeout=0.0) is None

    # A reply already dequeued by the publisher is invalidated before it can
    # publish if Abort lands during a rosbridge reconnect.
    _speak(engine, sim, centers, position, "What is your order?")
    token, _payload = _reply(engine)
    engine.abort()
    assert not engine.chat_input_is_current(token)

    # A reply still in the queue is discarded on Abort as well.
    assert engine.start("household_orders")
    _speak(engine, sim, centers, position, "What is your order?")
    engine.abort()
    assert engine.next_chat_input(timeout=0.0) is None


def test_reply_send_is_serialized_before_abort(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    position = centers[residents["alex"].prop]
    _speak(engine, sim, centers, position, "What is your order?")
    token, _payload = _reply(engine)

    abort_started = threading.Event()
    abort_done = threading.Event()
    sent = []
    abort_thread = None

    def abort_run():
        abort_started.set()
        engine.abort()
        abort_done.set()

    def publish_while_aborting():
        nonlocal abort_thread
        abort_thread = threading.Thread(target=abort_run)
        abort_thread.start()
        assert abort_started.wait(1.0)
        assert not abort_done.wait(0.02), "Abort crossed an in-flight old-run send"
        sent.append(token)

    assert engine.publish_chat_input_if_current(token, publish_while_aborting)
    assert abort_thread is not None
    abort_thread.join(timeout=1.0)
    assert abort_done.is_set()
    assert sent == [token]
    assert not engine.chat_input_is_current(token)


def test_chat_write_timeout_bounds_abort_delay(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    position = centers[residents["alex"].prop]
    _speak(engine, sim, centers, position, "What is your order?")
    token, _payload = _reply(engine)

    send_released = threading.Event()
    shutdown_calls = []

    class BlockingSocket:
        def shutdown(self, how):
            shutdown_calls.append(how)
            send_released.set()

    class BlockingConnection:
        socket = BlockingSocket()

        @staticmethod
        def send(_message):
            assert send_released.wait(1.0)
            raise OSError("socket was shut down")

    connection = BlockingConnection()
    send_started = threading.Event()
    send_errors = []

    def blocked_send():
        send_started.set()
        try:
            ChallengeChatBridge._send_with_timeout(connection, "frame", 0.02)
        except Exception as exc:  # noqa: BLE001 -- asserted below, across a thread boundary
            send_errors.append(exc)

    publisher = threading.Thread(target=engine.publish_chat_input_if_current, args=(token, blocked_send))
    publisher.start()
    assert send_started.wait(1.0)

    aborter = threading.Thread(target=engine.abort)
    aborter.start()
    aborter.join(timeout=1.0)
    publisher.join(timeout=1.0)

    assert not aborter.is_alive(), "Abort remained blocked behind a wedged chat write"
    assert not publisher.is_alive()
    assert len(send_errors) == 1 and isinstance(send_errors[0], TimeoutError)
    assert shutdown_calls == [socket.SHUT_RDWR]


def test_fast_chat_send_leaves_connection_open():
    shutdown_calls = []
    sent = []
    connection = SimpleNamespace(
        socket=SimpleNamespace(shutdown=lambda how: shutdown_calls.append(how)),
        send=lambda message: sent.append(message),
    )

    ChallengeChatBridge._send_with_timeout(connection, "frame", 0.02)
    time.sleep(0.03)

    assert sent == ["frame"]
    assert shutdown_calls == []


def test_robot_topic_cannot_forge_resident_confirmation(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    # This is the strongest JSON facsimile a robot-owned ROS publisher can
    # make. It cannot reproduce the environment's in-process source sentinel.
    engine.post_event(
        {
            "type": "resident_order_confirmed",
            "source": "resident_dialogue",
            "_source": "resident_dialogue",
            "resident": "alex",
        }
    )
    block = _tick(engine, sim, centers, position)

    assert block["active"]["state"] == "running"
    assert not block["active"]["goals"][0]["done"]


def test_roster_and_active_state_never_serialize_private_orders(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path)
    block = _tick(engine, sim, centers, (100.0, 100.0))
    public_json = json.dumps({"roster": engine.roster(), "state": block}, sort_keys=True)

    for resident in residents.values():
        assert resident.order not in public_json
        for readback in resident.accepted_readbacks:
            assert readback not in public_json
    assert "residents" not in block["active"]


def test_household_residents_use_distinct_visual_sources():
    props = load_props([REPO_ROOT / "sim" / "props"])
    residents = [props[f"resident_{name}"] for name in ("alex", "blake", "casey")]

    assert len({resident.mesh for resident in residents}) == 3
    assert len({resident.texture or f"{Path(resident.mesh).stem}_basecolor.png" for resident in residents}) == 3
    assert len({resident.viewer["glb"] for resident in residents}) == 3
    assert all(resident.collision == "hull" for resident in residents)
    assert all(resident.mesh_scale == 1.0 for resident in residents)
    assert all(resident.viewer["preNormalized"] is True for resident in residents)
    assert all("hulls" not in resident.viewer for resident in residents)
    assert all(resident.viewer["glb"] != "/models/human.glb" for resident in residents)


def test_kinematic_prop_uses_mocap_pose_without_a_freejoint():
    prop = Prop(name="resident", kinematic=True, rest_z=0.0)
    registry = PropRegistry({prop.name: prop})
    body_xml = registry.bodies_xml(visual_group=2, collision_group=3)

    assert 'mocap="true"' in body_xml
    assert "freejoint" not in body_xml

    model = SimpleNamespace(
        body=lambda _name: SimpleNamespace(id=0),
        body_mocapid=[0],
    )
    data = SimpleNamespace(mocap_pos=[[0.0, 0.0, 0.0]], mocap_quat=[[1.0, 0.0, 0.0, 0.0]])
    registry.bind(model)

    assert registry.drop_at(data, prop.name, 1.0, 2.0, math.pi / 2)
    assert data.mocap_pos[0] == [1.0, 2.0, 0.0]
    assert data.mocap_quat[0] == pytest.approx([math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)])
