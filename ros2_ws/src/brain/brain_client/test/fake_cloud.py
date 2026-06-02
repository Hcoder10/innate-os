"""
FakeCloud - a scriptable, deterministic stand-in for the innate-cloud-agent.

The real cloud agent's "brain" is a VLM: given an image it decides the next
primitive. That non-determinism makes it useless as a test oracle. FakeCloud
replaces the brain with a *script* the test author controls, while speaking the
exact same WebSocket protocol the robot expects. The robot stack cannot tell it
apart from the real cloud, so a green test predicts real-robot behavior for
everything except the VLM's choices.

It is pure Python (only `websockets`, already a robot dependency) and has NO ROS
dependency, so it can be used in two ways:

  Tier 1 (fast, plain colcon/pytest, no Docker):
    Point ws_client_node's `websocket_uri` param at FakeCloud.uri and launch the
    real ws_client_node + brain_client_node. Assert on the transcript (what the
    robot sent) and on ROS-observable side effects (ExecuteSkill goals, chat_out).

  Tier 2 (full-stack, Docker + innate-sim):
    Same FakeCloud, but the real stack drives the simulator. Assert on sim
    ground-truth pose in addition to the transcript.

Protocol reference (verified against ws_client_node.py / brain_client_node.py /
message_types.py as of this writing):

  Handshake:   robot -> auth                         ; cloud -> ready_for_image
  Register:    robot -> register_primitives_and_directive
                                                      ; cloud -> primitives_and_directive_registered
  Perception:  robot -> image | pose_image           ; cloud -> vision_agent_output (+ ready_for_image)
  Lifecycle:   robot -> primitive_activated|completed|failed|interrupted|feedback
                                                      ; cloud -> ready_for_image
  Chat:        robot -> chat_in                       ; (fast path; cloud may chat_out)

  next_task is dispatched inside vision_agent_output. The live cloud emits it with
  a "name" key, which brain_client_node hot-fixes to "type"; we replicate that
  exactly so the rename path is exercised. Each queued task is consumed by one
  perception message, so queueing [A, B] yields: image->A, (run/complete),
  image->B - i.e. a natural multi-step directive.
"""

import asyncio
import json
import threading
import uuid
from typing import Any

import websockets


def _vision_agent_output(next_task: dict | None) -> dict:
    """Build a protocol-valid vision_agent_output payload.

    All VisionAgentOutput fields must be present (the robot validates with
    pydantic); Optional ones may be None.
    """
    return {
        "type": "vision_agent_output",
        "payload": {
            "stop_current_task": False,
            "observation": "",
            "thoughts": "",
            "new_goal": None,
            "next_task": next_task,
            "anticipation": None,
            "to_tell_user": None,
        },
    }


class FakeCloud:
    """Scriptable fake cloud agent. Start it, point the robot at `.uri`, script
    the tasks it should dispatch, then assert on `.transcript`.

    Not thread-safe for scripting from multiple threads; script before the robot
    connects, or use the async inject helpers from the server's own loop.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        """:param port: 0 lets the OS pick a free port (read it back from `.port`)."""
        self._host = host
        self._port = port

        # Every message received from the robot, in order. The assertion surface.
        self.transcript: list[dict[str, Any]] = []

        # Tasks queued to dispatch when the robot is idle, one per perception.
        self._task_queue: list[dict] = []
        # True while a primitive is in flight (activated, not yet terminal).
        self._busy = False

        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws = None  # most-recent connected robot socket
        self._ready = threading.Event()
        # Notifies waiters (wait_for) that the transcript grew.
        self._cond = threading.Condition()

    # ----------------------------------------------------------------- scripting

    def script_task(
        self,
        name: str,
        inputs: dict | None = None,
        primitive_id: str | None = None,
    ) -> str:
        """Queue a primitive to dispatch on the next perception message.

        Returns the primitive_id (generated if not supplied) so the test can
        correlate it with the lifecycle messages the robot sends back.
        """
        pid = primitive_id or str(uuid.uuid4())
        # "name" (not "type") mirrors the live cloud; the robot renames it.
        self._task_queue.append({"name": name, "inputs": inputs or {}, "primitive_id": pid})
        return pid

    # -------------------------------------------------------------- observation

    def received_types(self) -> list[str]:
        with self._cond:
            return [m.get("type") for m in self.transcript]

    def wait_for(self, msg_type: str, timeout: float = 10.0, *, after: int = 0) -> dict:
        """Block until a message of `msg_type` appears in the transcript at index
        >= `after`. Returns it. Raises TimeoutError otherwise.

        `after` lets you wait for the *next* occurrence: pass len(transcript)
        before triggering the action you expect to produce the message.
        """
        deadline_msg = f"Timed out after {timeout}s waiting for '{msg_type}'"
        with self._cond:

            def _found():
                for m in self.transcript[after:]:
                    if m.get("type") == msg_type:
                        return m
                return None

            hit = self._cond.wait_for(lambda: _found() is not None, timeout=timeout)
            if not hit:
                raise TimeoutError(f"{deadline_msg}. Received: {[m.get('type') for m in self.transcript]}")
            return _found()

    # -------------------------------------------------------------- lifecycle

    @property
    def port(self) -> int:
        return self._port

    @property
    def uri(self) -> str:
        return f"ws://{self._host}:{self._port}"

    def start(self, timeout: float = 5.0):
        """Start the server in a background thread. Blocks until it is listening."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError("FakeCloud failed to start within timeout")
        return self

    def stop(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ----------------------------------------------------------------- internals

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._shutdown())
            self._loop.close()

    async def _serve(self):
        self._server = await websockets.serve(self._handler, self._host, self._port, max_size=16 * 1024 * 1024)
        # Resolve the OS-assigned port back onto the instance.
        self._port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

    async def _shutdown(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _send(self, message: dict):
        if self._ws is not None:
            await self._ws.send(json.dumps(message))

    async def _handler(self, websocket):
        self._ws = websocket
        try:
            async for raw in websocket:
                await self._on_message(raw)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _on_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        with self._cond:
            self.transcript.append(msg)
            self._cond.notify_all()

        mtype = msg.get("type")

        if mtype == "auth":
            await self._send({"type": "ready_for_image", "payload": {}})
        elif mtype == "register_primitives_and_directive":
            # The robot treats a missing/false "success" as a registration
            # FAILURE (brain_client_node.py:2268) and never activates the brain,
            # so the ack must affirm success.
            payload = msg.get("payload") or {}
            await self._send(
                {
                    "type": "primitives_and_directive_registered",
                    "payload": {
                        "success": True,
                        "count": len(payload.get("primitives") or []),
                        "directive_registered": True,
                    },
                }
            )
        elif mtype in ("image", "pose_image"):
            await self._handle_perception()
        elif mtype in (
            "primitive_activated",
            "primitive_completed",
            "primitive_failed",
            "primitive_interrupted",
            "primitive_feedback",
        ):
            # Mirror the real cloud: one primitive at a time. A primitive is in
            # flight from activation until a terminal result; no next_task is
            # dispatched in between.
            if mtype == "primitive_activated":
                self._busy = True
            elif mtype in ("primitive_completed", "primitive_failed", "primitive_interrupted"):
                self._busy = False
            # Keep the loop fed so the robot sends the next image.
            await self._send({"type": "ready_for_image", "payload": {}})
        # chat_in: fast path; recorded only.

    async def _handle_perception(self):
        # The real brain only issues a new task when idle.
        next_task = self._task_queue.pop(0) if self._task_queue and not self._busy else None
        await self._send(_vision_agent_output(next_task))
        await self._send({"type": "ready_for_image", "payload": {}})
