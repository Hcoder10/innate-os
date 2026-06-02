"""
Self-tests for FakeCloud - validate it speaks the robot<->cloud protocol
faithfully, with ZERO ROS and ZERO Docker. These guard the test double itself:
if the real protocol changes, these break before any integration test gives
false confidence.

A minimal "robot" client here mimics exactly what ws_client_node does on the
wire (auth, register, then the perception/lifecycle loop). It is NOT the real
node - it exists only to exercise FakeCloud's protocol surface.

Run:  pytest ros2_ws/src/brain/brain_client/test/test_fake_cloud_selftest.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import websockets  # noqa: E402
from fake_cloud import FakeCloud  # noqa: E402


async def _recv(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))


async def _send(ws, mtype, payload):
    await ws.send(json.dumps({"type": mtype, "payload": payload}))


def test_auth_handshake_returns_ready_for_image():
    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t", "client_version": "1.0.0"})
            msg = await _recv(ws)
            assert msg["type"] == "ready_for_image"

    with FakeCloud() as fake:
        asyncio.run(scenario())
    assert fake.received_types() == ["auth"]


def test_register_is_acknowledged():
    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t"})
            await _recv(ws)  # ready_for_image
            await _send(
                ws, "register_primitives_and_directive", {"primitives": [{"name": "x"}], "directive": "be helpful"}
            )
            msg = await _recv(ws)
            assert msg["type"] == "primitives_and_directive_registered"
            # The robot requires success=True or it treats registration as failed.
            assert msg["payload"]["success"] is True
            assert msg["payload"]["count"] == 1

    with FakeCloud() as fake:
        asyncio.run(scenario())


def test_scripted_task_dispatched_with_type_and_id():
    """A queued task is delivered inside vision_agent_output. The live cloud
    sends it under 'name'; verify that's what's on the wire (the robot renames
    it to 'type' downstream)."""

    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t"})
            await _recv(ws)
            await _send(ws, "image", {"image_b64": "", "robot_coords": {}})
            vao = await _recv(ws)
            assert vao["type"] == "vision_agent_output"
            task = vao["payload"]["next_task"]
            assert task is not None
            assert task["name"] == "navigate_to_position"
            assert task["primitive_id"] == pid
            assert task["inputs"] == {"x": 1.0, "y": 0.0, "theta": 0.0}
            # ready_for_image follows the vision output.
            assert (await _recv(ws))["type"] == "ready_for_image"

    with FakeCloud() as fake:
        pid = fake.script_task("navigate_to_position", {"x": 1.0, "y": 0.0, "theta": 0.0})
        asyncio.run(scenario())


def test_idle_when_no_task_queued():
    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t"})
            await _recv(ws)
            await _send(ws, "image", {"image_b64": ""})
            vao = await _recv(ws)
            assert vao["payload"]["next_task"] is None

    with FakeCloud() as fake:
        asyncio.run(scenario())


def test_multi_step_directive_runs_tasks_in_sequence():
    """Queue two tasks; with the robot activating+completing each, verify the
    natural cadence: image->A, run A, image->B, run B, image->idle."""

    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t"})
            await _recv(ws)
            dispatched = []
            for _ in range(3):
                await _send(ws, "image", {"image_b64": ""})
                task = (await _recv(ws))["payload"]["next_task"]
                await _recv(ws)  # ready_for_image
                if task is not None:
                    dispatched.append(task["name"])
                    # Robot runs the primitive: activate, then complete.
                    await _send(ws, "primitive_activated", {"primitive_id": task["primitive_id"]})
                    await _recv(ws)  # ready_for_image
                    await _send(ws, "primitive_completed", {"primitive_id": task["primitive_id"]})
                    await _recv(ws)  # ready_for_image
            assert dispatched == ["navigate_to_position", "wave"]

    with FakeCloud() as fake:
        fake.script_task("navigate_to_position", {"x": 1.0})
        fake.script_task("wave")
        asyncio.run(scenario())


def test_busy_suppresses_dispatch_until_complete():
    """While a primitive is in flight (activated, not yet terminal), perception
    yields no new task even though one is queued. After completion it dispatches."""

    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t"})
            await _recv(ws)
            await _send(ws, "image", {})
            task_a = (await _recv(ws))["payload"]["next_task"]
            await _recv(ws)
            assert task_a is not None and task_a["name"] == "task_a"

            # Robot activates A -> cloud is now busy.
            await _send(ws, "primitive_activated", {"primitive_id": task_a["primitive_id"]})
            await _recv(ws)
            # Image while busy -> no new task, though task_b is queued.
            await _send(ws, "image", {})
            assert (await _recv(ws))["payload"]["next_task"] is None
            await _recv(ws)

            # Complete A -> idle; next image dispatches task_b.
            await _send(ws, "primitive_completed", {"primitive_id": task_a["primitive_id"]})
            await _recv(ws)
            await _send(ws, "image", {})
            task_b = (await _recv(ws))["payload"]["next_task"]
            assert task_b is not None and task_b["name"] == "task_b"

    with FakeCloud() as fake:
        fake.script_task("task_a")
        fake.script_task("task_b")
        asyncio.run(scenario())


def test_transcript_records_lifecycle_in_order():
    async def scenario():
        async with websockets.connect(fake.uri) as ws:
            await _send(ws, "auth", {"token": "t"})
            await _recv(ws)
            await _send(ws, "image", {"image_b64": ""})
            await _recv(ws)  # vao
            await _recv(ws)  # ready
            await _send(ws, "primitive_activated", {"primitive_id": "x"})
            await _recv(ws)
            await _send(ws, "primitive_failed", {"primitive_id": "x"})
            await _recv(ws)

    with FakeCloud() as fake:
        asyncio.run(scenario())
    assert fake.received_types() == [
        "auth",
        "image",
        "primitive_activated",
        "primitive_failed",
    ]
