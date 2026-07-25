# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The local brain: a plain agent loop over Gemini with the robot's skills as tools.

This replaces the cloud brain and its WebSocket protocol. Each turn:

    look   — snapshot the camera, pose, running skill, and queued events
    think  — one Gemini call (on a worker thread, the executor never blocks)
    act    — speak the reply, start a skill, or stop the running one

Turns run back-to-back when something happened (user spoke, a skill finished)
and on a slow heartbeat otherwise, so the robot keeps watching the scene.
"""

from __future__ import annotations

import base64
import json
import math
import os
import queue
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
        self._node = node
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

        self._events: list[dict] = []  # pending {"text", "image"} for the next turn
        self._events_in_turn: list[dict] = []  # drained into the in-flight turn; re-queued if it fails
        self._turn_in_flight = False
        self._next_turn_due = 0.0  # monotonic; 0 = turn wanted now
        self._pose_at_capture = None
        self._frame_at_capture: bytes | None = None
        self._pitch_at_capture = 0.0
        self._tool_map: dict[str, str] = {}  # gemini function name -> skill id
        self._error_streak = 0
        self._lidar_error_reported = False
        self._activated_at = 0.0
        self._timer = None
        self._turn_count = 0
        self._turn_started_at = 0.0
        self._last_snapshot_at = 0.0

        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._result_guard = node.create_guard_condition(self._finish_turn)

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
        self._activated_at = time.monotonic()
        self._next_turn_due = 0.0
        self._error_streak = 0
        if self._timer is None:
            self._timer = self._node.create_timer(0.2, self._tick)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._node.destroy_timer(self._timer)
            self._timer = None
        self._events.clear()

    def reset(self) -> None:
        """Forget the conversation (directive switch / brain reset)."""
        if self._session is not None:
            self._session.clear()
        self._events.clear()

    # ================= trace (the monitor's window into the loop) =================
    def _trace(self, event: str, **fields) -> None:
        """Publish one JSON telemetry event on /brain/trace (no-op when unwired).

        Everything here runs on the executor thread; the worker thread never
        traces directly — its results are traced when they land in
        :meth:`_finish_turn`.
        """
        if self._trace_sink is None:
            return
        self._trace_sink(json.dumps({"ev": event, "t": time.time(), **fields}))

    # ================= events (executor thread) =================
    def add_event(self, text: str, image: bytes | None = None, kind: str = "info") -> None:
        """Queue something that happened; the next turn starts as soon as possible."""
        self._events.append({"text": text, "image": image, "kind": kind})
        self._next_turn_due = 0.0
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

    # ================= the loop =================
    def _tick(self) -> None:
        self._check_lidar_health()
        self._snapshot()
        if not self._state.is_brain_active or not self.available or self._turn_in_flight:
            return
        # The due time is authoritative: new events reset it to 0 (add_event),
        # while events re-queued after an inference failure keep the backoff.
        if time.monotonic() < self._next_turn_due:
            return
        if self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC) is None:
            return
        self._start_turn()

    def _start_turn(self) -> None:
        text, images = self._observe()
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
        threading.Thread(
            target=self._think, args=(self._session.generation, user_message, tools, system), daemon=True
        ).start()

    def _observe(self) -> tuple[str, list[bytes]]:
        """Drain pending events and snapshot the world into one turn input."""
        events, self._events = self._events, []
        self._events_in_turn = events  # held so a failed turn can give them back

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

    def _think(self, generation, user_message, tools, system) -> None:
        """Worker thread: just the network call; the result hops back to the executor."""
        try:
            response = self._session.generate(user_message, tools, system)
            self._results.put((generation, user_message, response, None))
        except Exception as error:  # network/API failure -> paced retry on the executor
            self._results.put((generation, user_message, None, error))
        self._result_guard.trigger()

    def _finish_turn(self) -> None:
        """Guard-condition callback (executor thread): absorb and act on the response."""
        while True:
            try:
                generation, user_message, response, error = self._results.get_nowait()
            except queue.Empty:
                return
            self._turn_in_flight = False
            latency = time.monotonic() - self._turn_started_at
            if self._session is None or generation != self._session.generation or not self._state.is_brain_active:
                # Directive switched, brain reset, or deactivated while this
                # turn was thinking: drop the whole exchange so the history
                # carries nothing from a moment that no longer exists.
                self._events_in_turn = []
                self._trace("turn_dropped", turn=self._turn_count, latency=round(latency, 2))
                continue
            if error is not None:
                self._handle_error(error)
                continue
            self._events_in_turn = []
            if self._error_streak > 0:
                self._error_streak = 0
                self._chat.emit_system("✅ Brain inference recovered.")
            decision = self._session.absorb(user_message, response)
            outcomes = self._apply(decision)
            interval = (
                self._config.supervision_turn_interval
                if self._state.primitive_running
                else self._config.idle_turn_interval
            )
            self._next_turn_due = time.monotonic() + interval
            self._trace(
                "turn_end",
                turn=self._turn_count,
                latency=round(latency, 2),
                thoughts=decision.thoughts,
                speech=decision.speech,
                calls=[{"name": call.name, "args": call.args, "outcome": outcome} for call, outcome in outcomes],
                history=self._session.history_len,
                next_in=round(interval, 1),
            )

    def _handle_error(self, error: Exception) -> None:
        self._error_streak += 1
        self._logger.error(f"[Brain] Gemini call failed ({self._error_streak}x): {error}")
        if self._error_streak == 1:
            self._chat.emit_system(f"⚠️ Brain inference failed: {error} — retrying.")
        # Give the failed turn's events back to the queue so the retry re-sends
        # them — a fresh heartbeat observation alone would silently drop the
        # user's command or a skill result.
        self._events = self._events_in_turn + self._events
        self._events_in_turn = []
        backoff = min(5.0 * self._error_streak, 30.0)
        self._next_turn_due = time.monotonic() + backoff
        self._trace("turn_error", turn=self._turn_count, error=str(error), streak=self._error_streak, backoff=backoff)

    def _snapshot(self) -> None:
        """Trace the loop's live state once a second (the monitor's heartbeat)."""
        if self._trace_sink is None or time.monotonic() - self._last_snapshot_at < 1.0:
            return
        self._last_snapshot_at = time.monotonic()
        running = self._state.primitive_running
        self._trace(
            "snapshot",
            active=self._state.is_brain_active,
            backend=self.backend,
            model=self._config.gemini_model,
            turn=self._turn_count,
            in_flight=self._turn_in_flight,
            thinking_for=round(time.monotonic() - self._turn_started_at, 1) if self._turn_in_flight else 0,
            queued=[{"kind": e["kind"], "text": e["text"][:200]} for e in self._events],
            next_in=max(0.0, round(self._next_turn_due - time.monotonic(), 1)),
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
