# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The local brain: a plain agent loop over Gemini with the robot's skills as tools.

This replaces the cloud brain and its WebSocket protocol. Each turn:

    look   — snapshot the camera, pose, running skill, and queued events
    think  — one Gemini call (awaited on a worker thread, nothing else blocks)
    act    — speak the reply, start a skill, or stop the running one

The whole agent is one sequential coroutine (:meth:`BrainAgent._turns`) on a
dedicated asyncio loop thread, which buys two structural guarantees:

* **Cancellation is the only stop mechanism.** Deactivation and reset cancel
  the task; a turn still thinking unwinds at its await and can never absorb
  its response — there is no stale-turn bookkeeping because a cancelled await
  does not return.
* **Turns are transactional.** Queued events are only consumed when a turn
  commits its exchange into history. A failed or cancelled turn leaves them
  queued, so the retry (or the next activation) re-sends them.

Threading contract: ROS callbacks (executor thread) only queue events and wake
the loop; observing, history, and acting on decisions all happen in the
coroutine. Publishing and sending action goals are thread-safe in rclpy;
creating/destroying ROS entities is not, and stays on the executor (the
lifecycle starts/stops the camera and pose subscriptions).

Turns run back-to-back when something happened (user spoke, a skill finished)
and on a slow heartbeat otherwise, so the robot keeps watching the scene.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import threading
import time
import uuid

from brain_client.brain import grounding
from brain_client.brain.gemini import (
    GO_TO_POINT,
    STOP_SKILL,
    WAIT,
    GeminiSession,
    assign_tool_names,
    build_tools,
    direct_transport,
    proxy_transport,
)
from brain_client.brain.prompt import build_system_prompt
from brain_client.perception import pose as pose_math

_NAV_TO_POSITION = "innate-os/navigate_to_position"

# A camera frame older than this means the camera feed is broken; don't think on it.
_FRESH_FRAME_SEC = 3.0

# Thought summaries can run to pages; the chat panel wants a glimpse, not a log.
_MAX_THOUGHT_CHARS = 600


def _trim_thoughts(thoughts: str) -> str:
    if len(thoughts) <= _MAX_THOUGHT_CHARS:
        return thoughts
    return thoughts[:_MAX_THOUGHT_CHARS].rsplit(" ", 1)[0] + " …"


def _clean_speech(speech: str | None) -> str | None:
    """Drop unspeakable output: placeholders and leaked tool-call narration."""
    if not speech:
        return None
    # gemini-3 preview sometimes appends "Calling tool default_api:..." to its text.
    speech = re.sub(r"Calling tool\b.*", "", speech, flags=re.DOTALL).strip()
    if not re.search(r"[a-zA-Z0-9]", speech):
        return None
    return speech


class BrainAgent:
    def __init__(
        self,
        node,
        state,
        config,
        *,
        camera,
        pose_tracker,
        runner,
        roster,
        chat,
        gaze,
        proxy=None,
        scan_health=None,
        trace=None,
    ):
        # The node is only a logger source now: the loop owns its own asyncio
        # loop, so there are no ROS timers or guard conditions to create.
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._camera = camera
        self._pose = pose_tracker
        self._runner = runner
        self._roster = roster
        self._chat = chat
        self._gaze = gaze
        self._scan_health = scan_health
        self._trace_sink = trace  # callable(json_str) publishing /brain/trace; None = tracing off

        transport, self.backend = self._pick_transport(proxy)
        self.available = transport is not None
        self._session = (
            GeminiSession(
                transport,
                model=config.gemini_model,
                thinking_level=config.gemini_thinking_level,
                max_history=config.history_max_entries,
                max_image_turns=config.history_max_image_turns,
            )
            if self.available
            else None
        )

        # Pending {"text", "image", "kind"} stimuli. The executor appends to the
        # tail; the coroutine pops from the head — and only when a turn commits.
        self._events: list[dict] = []
        self._pose_at_capture = None
        self._frame_at_capture: bytes | None = None
        self._pitch_at_capture = 0.0
        self._tool_map: dict[str, str] = {}  # gemini function name -> skill id
        self._error_streak = 0
        self._lidar_error_reported = False
        self._activated_at = 0.0
        self._turn_count = 0
        self._turn_started_at = 0.0
        self._turn_in_flight = False
        self._pause_until = 0.0  # monotonic deadline of the current between-turns pause

        # The agent's own asyncio loop on a dedicated thread. ROS callbacks wake
        # it via call_soon_threadsafe; start()/stop() spawn and cancel the task.
        self._loop = asyncio.new_event_loop()
        self._wake = asyncio.Event()  # loop-thread only; set via call_soon_threadsafe
        self._task = None  # concurrent.futures.Future for the running root task
        self._done = threading.Event()  # set when the root task has fully unwound
        self._done.set()
        self._thread = threading.Thread(target=self._loop.run_forever, name="brain-agent", daemon=True)
        self._thread.start()

    @staticmethod
    def _pick_transport(proxy):
        """Prefer the Innate proxy (the managed path); GEMINI_API_KEY is the dev override."""
        if proxy is not None and proxy.is_available():
            return proxy_transport(proxy), "innate-proxy"
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if api_key:
            return direct_transport(api_key), "gemini-direct"
        return None, "unconfigured"

    # ================= lifecycle =================
    def start(self) -> None:
        if not self.available:
            self._chat.emit_system(
                "⚠️ The brain has no way to reach Gemini — configure the Innate proxy "
                "(INNATE_SERVICE_KEY) or set GEMINI_API_KEY in innate-os/.env and restart."
            )
        if self._task is not None:
            return
        self._activated_at = time.monotonic()
        self._error_streak = 0
        self._spawn()

    def stop(self) -> None:
        """Cancel the loop task and wait for it to unwind.

        Synchronous by design: when this returns, no turn is thinking and no
        decision will be acted on, so deactivation can safely interrupt the
        running skill and reset can safely clear the history.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            if not self._done.wait(timeout=5.0):
                self._logger.error("[Brain] Agent loop did not unwind within 5s")
        self._events.clear()

    def reset(self) -> None:
        """Forget the conversation (directive switch / brain reset).

        Restarts the loop task: a turn thinking under the old directive is
        cancelled with it, so nothing from the old conversation can reach the
        fresh history.
        """
        was_running = self._task is not None
        self.stop()
        if self._session is not None:
            self._session.clear()
        if was_running and self._state.is_brain_active:
            self._spawn()

    def shutdown(self) -> None:
        """Stop the loop thread for good (node teardown)."""
        self.stop()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)
        if not self._thread.is_alive():
            self._loop.close()

    def _spawn(self) -> None:
        self._done = threading.Event()
        self._task = asyncio.run_coroutine_threadsafe(self._run(self._done), self._loop)

    # ================= trace (the monitor's window into the loop) =================
    def _trace(self, event: str, **fields) -> None:
        """Publish one JSON telemetry event on /brain/trace (no-op when unwired)."""
        if self._trace_sink is None:
            return
        self._trace_sink(json.dumps({"ev": event, "t": time.time(), **fields}))

    # ================= events (executor thread) =================
    def add_event(self, text: str, image: bytes | None = None, kind: str = "info") -> None:
        """Queue something that happened; the loop wakes for an immediate turn."""
        self._events.append({"text": text, "image": image, "kind": kind})
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)
        self._trace("event", kind=kind, text=text, image=image is not None)

    def on_user_message(self, text: str) -> None:
        self.add_event(f'The user says: "{text}"', kind="user")

    def on_custom_input(self, data: dict) -> None:
        device = data.get("input_device", "unknown")
        self.add_event(f"Input from {device}: {json.dumps(data)}")

    def on_skill_event(self, status: str, skill_name: str, detail: str | None = None) -> None:
        line = f"Skill {skill_name} {status}"
        if detail:
            line += f": {detail}"
        self.add_event(line)

    def on_skill_feedback(self, skill_name: str, feedback: str, image: bytes | None = None) -> None:
        self.add_event(f"Update from running skill {skill_name}: {feedback}", image=image)

    # ================= the loop (coroutine, loop thread) =================
    async def _run(self, done: threading.Event) -> None:
        """Root task: the turn loop plus the 1 Hz telemetry heartbeat."""
        children = [asyncio.ensure_future(self._telemetry())]
        if self.available:
            children.append(asyncio.ensure_future(self._turns()))
        try:
            await asyncio.gather(*children)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A bug, not a transport failure (those are handled per turn).
            self._logger.error(f"[Brain] Agent loop crashed: {error!r}")
            self._chat.emit_system(f"⚠️ Brain loop crashed: {error!r} — stop and start the brain to recover.")
        finally:
            for child in children:
                child.cancel()
            done.set()

    async def _turns(self) -> None:
        """look → think → act, forever. Every await is a cancel point."""
        while True:
            while self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC) is None:
                await asyncio.sleep(0.2)  # the feed is down; don't think blind
            await self._turn()
            if not self._events:
                await self._pause(self._interval())

    async def _turn(self) -> None:
        events = list(self._events)  # peek — consumed only if the turn commits
        text, images = self._observe(events)
        user_message = GeminiSession.user_message(text, images)
        tools = self._build_tools()
        system = build_system_prompt(self._directive_prompt())
        self._pose_at_capture = self._pose.current_pose_xyt()

        if self._state.log_everything:
            self._logger.info(f"[Brain] Turn input:\n{text}")

        self._turn_count += 1
        self._turn_started_at = time.monotonic()
        self._trace(
            "turn_start",
            turn=self._turn_count,
            input=text,
            images=len(images),
            tools=[d["name"] for d in tools[0]["functionDeclarations"]],
            history=self._session.history_len,
            frame=base64.b64encode(self._frame_at_capture).decode(),
        )

        self._turn_in_flight = True
        try:
            # The only blocking call, on a worker thread; generate() only reads
            # the history, so it never races the mutation below. Cancellation
            # unwinds HERE — the orphaned HTTP call finishes on its worker
            # thread and its result is dropped by asyncio.
            response = await asyncio.to_thread(self._session.generate, user_message, tools, system)
        except asyncio.CancelledError:
            self._turn_in_flight = False
            self._trace(
                "turn_dropped", turn=self._turn_count, latency=round(time.monotonic() - self._turn_started_at, 2)
            )
            raise
        except Exception as error:
            self._turn_in_flight = False
            self._error_streak += 1
            self._logger.error(f"[Brain] Gemini call failed ({self._error_streak}x): {error}")
            if self._error_streak == 1:
                self._chat.emit_system(f"⚠️ Brain inference failed: {error} — retrying.")
            backoff = min(5.0 * self._error_streak, 30.0)
            self._trace(
                "turn_error", turn=self._turn_count, error=str(error), streak=self._error_streak, backoff=backoff
            )
            # Nothing was consumed, so the retry re-sends the same events; an
            # event arriving on top of them cuts the backoff short.
            await self._pause(backoff, seen=len(events))
            return
        self._turn_in_flight = False

        latency = time.monotonic() - self._turn_started_at
        if self._error_streak > 0:
            self._error_streak = 0
            self._chat.emit_system("✅ Brain inference recovered.")
        if not self._state.is_brain_active:
            # Deactivation raced this turn's response (the flag flips just
            # before stop() cancels us): drop the whole exchange.
            self._trace("turn_dropped", turn=self._turn_count, latency=round(latency, 2))
            return
        decision = self._session.absorb(user_message, response)
        del self._events[: len(events)]  # commit: consume exactly what this turn saw
        outcomes = self._apply(decision)
        self._trace(
            "turn_end",
            turn=self._turn_count,
            latency=round(latency, 2),
            thoughts=decision.thoughts,
            speech=decision.speech,
            calls=[{"name": call.name, "args": call.args, "outcome": outcome} for call, outcome in outcomes],
            history=self._session.history_len,
            next_in=round(self._interval(), 1),
        )

    def _interval(self) -> float:
        return (
            self._config.supervision_turn_interval
            if self._state.primitive_running
            else self._config.idle_turn_interval
        )

    async def _pause(self, seconds: float, *, seen: int = 0) -> None:
        """Sleep up to ``seconds``; the queue growing past ``seen`` events ends it early."""
        self._wake.clear()
        if len(self._events) > seen:
            return  # queued while we weren't looking — don't wait at all
        self._pause_until = time.monotonic() + seconds
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), seconds)
        finally:
            self._pause_until = 0.0

    def _observe(self, events: list[dict]) -> tuple[str, list[bytes]]:
        """Snapshot the world + the given (peeked) events into one turn input."""
        status = f"[t+{int(time.monotonic() - self._activated_at)}s]"
        pose = self._pose.current_pose_xyt()
        if pose is not None:
            status += f" pose: x={pose[0]:.2f}m y={pose[1]:.2f}m heading={math.degrees(pose[2]):.0f}°"
        running = self._state.primitive_running
        if running:
            status += f" | running skill: {running['primitive_name']}"

        lines = [status]
        if running:
            guidance = self._running_skill_guidance(running.get("skill_id"))
            if guidance:
                lines.append(f"(guidance while this skill runs: {guidance})")
        lines += [f"- {event['text']}" for event in events]

        # Snapshot the frame + head pitch together: go_to_point projections must
        # use the geometry of the exact frame the model pointed into.
        self._frame_at_capture = self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC)
        self._pitch_at_capture = self._camera.current_head_pitch
        images = [self._frame_at_capture]
        arm_jpeg = self._camera.fresh_arm_jpeg(_FRESH_FRAME_SEC)
        if arm_jpeg is not None:
            images.append(arm_jpeg)
            lines.append("(second image is the arm wrist camera)")
        images += [event["image"] for event in events if event["image"]]
        return "\n".join(lines), images

    async def _telemetry(self) -> None:
        """1 Hz: lidar health surfacing + the monitor's snapshot heartbeat."""
        while True:
            self._check_lidar_health()
            self._snapshot()
            await asyncio.sleep(1.0)

    def _snapshot(self) -> None:
        """Trace the loop's live state (the monitor's heartbeat)."""
        if self._trace_sink is None:
            return
        running = self._state.primitive_running
        self._trace(
            "snapshot",
            active=self._state.is_brain_active,
            backend=self.backend,
            model=self._config.gemini_model,
            turn=self._turn_count,
            in_flight=self._turn_in_flight,
            thinking_for=round(time.monotonic() - self._turn_started_at, 1) if self._turn_in_flight else 0,
            queued=[{"kind": e["kind"], "text": e["text"][:200]} for e in list(self._events)],
            next_in=max(0.0, round(self._pause_until - time.monotonic(), 1)) if self._pause_until else 0.0,
            streak=self._error_streak,
            running=running["primitive_name"] if running else None,
            history=self._session.history_len if self._session else 0,
            uptime=round(time.monotonic() - self._activated_at, 0) if self._state.is_brain_active else 0,
        )

    # ================= acting on a decision =================
    def _apply(self, decision) -> list:
        if decision.thoughts:
            self._chat.emit("robot_thoughts", _trim_thoughts(decision.thoughts), speak=False)
        speech = _clean_speech(decision.speech)
        if speech and any(event["kind"] == "user" for event in self._events):
            # The user said something newer while this turn was thinking —
            # voicing an answer to the previous moment is how the robot ends
            # up talking over the conversation. The next turn sees both.
            self._logger.info(f"[Brain] Speech suppressed (newer user message pending): {speech[:60]!r}")
            speech = None
        if speech:
            self._chat.emit("robot", speech, speak=False)
            self._chat.speak(speech, replace_pending=True)
        outcomes = [(call, self._execute(call)) for call in decision.calls]
        self._session.add_tool_outcomes(outcomes)
        return outcomes

    def _execute(self, call) -> str:
        """Run one tool call; the returned string is the model-facing outcome."""
        self._logger.info(f"[Brain] Tool call: {call.name}({call.args})")
        if call.name == WAIT:
            return "ok"
        if call.name == STOP_SKILL:
            if self._runner.has_active_goal:
                self._runner.cancel_active_goal()
                return "stopping — you will get an event when it has stopped"
            if self._state.primitive_running is not None:
                # A run this client didn't start (webapp/CLI manual run): the
                # skills server's owner-agnostic cancel is the only handle.
                if self._runner.cancel_external():
                    return "stopping — you will get an event when it has stopped"
                return "could not stop it — the skills server is unreachable"
            return "no skill is running"
        if self._state.primitive_running is not None:
            return "rejected — another skill is already running; stop it first"
        if call.name == GO_TO_POINT:
            return self._go_to_point(call.args)

        skill_id = self._tool_map.get(call.name) or self._state.registry.resolve_skill_id(call.name)
        if skill_id is None:
            return f"unknown skill '{call.name}'"

        inputs = self._compensated(skill_id, dict(call.args))
        self._gaze.pause()
        self._runner.start_task(skill_id, f"local-{uuid.uuid4().hex[:8]}", inputs)
        return "started — you will get an event when it finishes"

    def _go_to_point(self, args: dict) -> str:
        """Project the pointed-at floor pixel into a local navigate_to_position goal."""
        nav_id = self._state.registry.resolve_skill_id(_NAV_TO_POSITION)
        if nav_id is None:
            return "rejected — navigate_to_position is not available"
        if self._frame_at_capture is None:
            return "rejected — no camera frame to ground the point in"
        try:
            v_norm, u_norm = float(args["y"]), float(args["x"])
        except (KeyError, TypeError, ValueError):
            return "rejected — give integer y and x in 0-1000 image coordinates"

        floor = grounding.pixel_to_floor(
            u_norm,
            v_norm,
            frame_jpeg=self._frame_at_capture,
            vertical_fov_deg=self._config.vertical_fov,
            pitch_deg=self._pitch_at_capture,
            cam_height=self._config.height_cam,
            cam_forward=self._config.x_cam,
        )
        if floor is None:
            return "rejected — that point is at or above the horizon; point at the floor"

        inputs = self._compensated(nav_id, grounding.approach_goal(*floor))
        self._logger.info(
            f"[Brain] go_to_point ({v_norm:.0f},{u_norm:.0f}) -> floor ({floor[0]:.2f}, {floor[1]:.2f})m, "
            f"goal ({inputs['x']:.2f}, {inputs['y']:.2f}, {inputs['theta_degrees']:.0f}°) pitch={self._pitch_at_capture:.0f}°"
        )
        self._gaze.pause()
        self._runner.start_task(nav_id, f"local-{uuid.uuid4().hex[:8]}", inputs)
        return (
            f"driving to the floor point {math.hypot(*floor):.1f}m away (stopping ~{grounding.STANDOFF_M}m short) "
            "— you will get an event when it finishes"
        )

    def _compensated(self, skill_id: str, inputs: dict) -> dict:
        """Ground a nav goal in the robot's current pose before sending it."""
        if skill_id != _NAV_TO_POSITION:
            return inputs
        current = self._pose.current_pose_xyt()
        if not inputs.get("local_frame", False):
            # In mapfree mode there is no map frame; the only absolute frame
            # the model knows is its pose readout's (odom). Re-base the goal
            # onto the robot and run it locally — a map-frame goal would only
            # die at the inactive map planner.
            if not self._pose.is_mapfree or current is None:
                return inputs
            rebased = pose_math.absolute_to_local_nav_command(inputs, current)
            self._logger.info(f"[Brain] mapfree: absolute goal re-based to local: {rebased}")
            return rebased
        # Local goals are relative to the frame the model saw; re-express them
        # if the robot moved since that frame was captured.
        if self._pose_at_capture is None or current is None:
            return inputs
        delta = pose_math.compute_pose_delta(self._pose_at_capture, current)
        return pose_math.adjust_local_nav_command(inputs, delta)

    # ================= helpers =================
    def _build_tools(self):
        running = self._state.primitive_running
        if running is not None:
            return build_tools([], running["primitive_name"])
        active_ids = set(self._roster.active_skill_ids())
        skills = [meta for meta in self._state.registry.metadata if meta["id"] in active_ids]
        self._tool_map = {name: meta["id"] for name, meta in assign_tool_names(skills)}
        return build_tools(skills, None, can_go_to_point=_NAV_TO_POSITION in active_ids)

    def _directive_prompt(self) -> str | None:
        directive = self._state.current_directive
        return directive.get_prompt() if directive is not None else None

    def _running_skill_guidance(self, skill_id: str | None) -> str:
        stub = self._state.registry.primitives.get(skill_id) if skill_id else None
        return stub.guidelines_when_running().strip() if stub else ""

    def _check_lidar_health(self) -> None:
        """Surface a clear chat error when the lidar stops publishing (INN-474)."""
        if self._scan_health is None or self._config.simulator_mode or self._pose.is_mapfree:
            self._lidar_error_reported = False
            return
        problem = self._scan_health.stale_problem()
        if problem is not None:
            if not self._lidar_error_reported:
                self._lidar_error_reported = True
                self._logger.error(f"[Brain] LiDAR not responding: {problem}")
                self._chat.emit_system(
                    f"⚠️ LiDAR is not responding ({problem}). The robot cannot localize or navigate "
                    "until it recovers. Check that the LiDAR is connected and spinning."
                )
        elif self._lidar_error_reported:
            self._lidar_error_reported = False
            self._logger.info("[Brain] LiDAR scan data restored.")
            self._chat.emit_system("✅ LiDAR is publishing again. Localization should recover shortly.")
