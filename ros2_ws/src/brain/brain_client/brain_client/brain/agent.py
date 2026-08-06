# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The local brain: look → think → act, one turn at a time.

The whole agent is one sequential coroutine on a dedicated loop thread
(:class:`~brain_client.brain.loop.LoopThread`), which buys two guarantees:

* **Cancellation is the only stop mechanism.** Deactivation and reset cancel
  the task; a turn still thinking unwinds at its await and can never absorb
  its response.
* **Turns are transactional.** Events are consumed only when a turn commits,
  so a failed or abandoned turn re-sends them. This is what makes abandoning
  free: user speech aborts any turn that has not yet begun to speak, and the
  rerun sees everything it saw plus the new message — a correction prevents a
  stale action instead of cancelling it after it started.

Threading contract: ROS callbacks (executor thread) only queue events and wake
the loop; observing, history, and acting all happen in the coroutine.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import time
import uuid

from brain_client.brain import grounding
from brain_client.brain.gemini import (
    GO_TO_POINT_IN_VIEW,
    STOP_SKILL,
    WAIT,
    GeminiSession,
    assign_tool_names,
    build_tools,
    pick_transport,
)
from brain_client.brain.loop import LoopThread
from brain_client.brain.prompt import build_system_prompt
from brain_client.perception import pose as pose_math
from brain_client.perception.scan_health import ScanHealthReporter
from brain_client.transport.chat import SpeechStreamer

_NAV_TO_POSITION = "innate-os/navigate_to_position"

# A camera frame older than this means the feed is broken; don't think blind.
_FRESH_FRAME_SEC = 3.0

# While the loop can't turn (feed down), only this many queued events are kept.
_MAX_EVENTS_WHILE_BLIND = 30

# Frame label for the arm wrist camera: these frames are latest-only in history
# (a stale gripper close-up reads as current grasp state).
_WRIST_LABEL = "wrist camera"


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
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._camera = camera
        self._pose = pose_tracker
        self._runner = runner
        self._roster = roster
        self._chat = chat
        self._gaze = gaze
        self._trace_sink = trace  # callable(json_str) publishing /brain/trace; None = tracing off
        self._lidar = ScanHealthReporter(
            scan_health, pose_tracker, chat, self._logger, enabled=not config.simulator_mode
        )

        transport, self.backend = pick_transport(proxy)
        self._session = (
            GeminiSession(
                transport,
                model=config.gemini_model,
                thinking_level=config.gemini_thinking_level,
                max_history=config.history_max_entries,
                max_image_turns=config.history_max_image_turns,
            )
            if transport is not None
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
        self._activated_at = 0.0
        self._turn_count = 0
        self._turn_started_at = 0.0
        self._turn_in_flight = False
        self._speaker: SpeechStreamer | None = None  # the in-flight turn's streamer (loop reads .spoke)
        self._pause_until = 0.0  # monotonic deadline of the current between-turns pause

        self._runtime = LoopThread("brain-agent")
        self._new_event = asyncio.Event()  # something was queued (loop thread; set via runtime.post)
        self._user_spoke = asyncio.Event()  # like _new_event, but only user speech sets it

        if self._session is not None:
            # The observability tap: the exact request body, straight off the
            # wire path (fires on generate's worker thread, like add_event's
            # executor-thread traces). The monitor renders it verbatim.
            self._session.on_request = lambda body: self._trace("turn_request", turn=self._turn_count, body=body)

    @property
    def available(self) -> bool:
        """Whether the brain can reach Gemini — true exactly when a session exists.

        Derived rather than stored so the two can never disagree: every guard
        that reads this also narrows ``_session`` for the type checker.
        """
        return self._session is not None

    # ================= lifecycle =================
    def start(self) -> None:
        if not self.available:
            self._chat.emit_system(
                "⚠️ The brain has no way to reach Gemini — configure the Innate proxy "
                "(INNATE_SERVICE_KEY) or set GEMINI_API_KEY in innate-os/.env and restart."
            )
        if self._runtime.running:
            return
        self._activated_at = time.monotonic()
        self._error_streak = 0
        self._runtime.spawn(self._loop())

    def stop(self) -> bool:
        """Synchronous: when this returns True, no turn is thinking and none will act."""
        unwound = self._runtime.cancel()
        if not unwound:
            self._logger.error("[Brain] Agent loop did not unwind within 5s")
        self._events.clear()
        return unwound

    def reset(self) -> None:
        """Forget the conversation; a turn thinking under the old one dies with it."""
        was_running = self._runtime.running
        if not self.stop():
            # The old loop is stuck mid-unwind: spawning a second one over it
            # would interleave two conversations. Stay down instead.
            self._chat.emit_system("⚠️ Brain loop is stuck — stop and start the brain to recover.")
            return
        if self._session is not None:
            self._session.clear()
        if was_running and self._state.is_brain_active:
            self._runtime.spawn(self._loop())

    def shutdown(self) -> None:
        self.stop()
        self._runtime.shutdown()

    # ================= the loop =================
    async def _loop(self) -> None:
        """Root task: turns forever, with the 1 Hz telemetry heartbeat alongside.

        A crash (a bug, not a transport failure — those back off per turn) is
        reported in chat and leaves the loop down until the brain is restarted.
        """
        session = self._session
        if session is None:
            return await self._heartbeat()  # no transport: telemetry only, no turns
        heartbeat = asyncio.ensure_future(self._heartbeat())
        turn = spoke = None
        reruns = 0
        try:
            while True:
                await self._await_camera()

                # Every turn races the user's voice. Abandoning is free —
                # nothing is consumed until a turn commits, so the rerun sees
                # everything this one saw plus the new message — with two
                # bounds: a turn that began speaking finishes what it says,
                # and the third rerun runs to completion, so nonstop speech
                # cannot starve the loop.
                self._user_spoke.clear()
                turn = asyncio.ensure_future(self._turn(session))
                spoke = asyncio.ensure_future(self._user_spoke.wait())
                await asyncio.wait((turn, spoke), return_when=asyncio.FIRST_COMPLETED)
                spoke.cancel()
                speaker = self._speaker  # _turn publishes it before its first await
                if not turn.done() and speaker is not None and not speaker.spoke and reruns < 2:
                    self._trace("turn_preempted", turn=self._turn_count, after=self._elapsed())
                    speaker.mute()  # the orphaned reply must not keep talking
                    turn.cancel()
                    await asyncio.wait({turn})  # fully unwound before the rerun looks
                    reruns += 1
                    continue
                await turn  # committed (or holding the floor): run to completion
                reruns = 0
                if not self._events:
                    await self._pause(self._interval())
        except Exception as error:
            self._logger.error(f"[Brain] Agent loop crashed: {error!r}")
            self._chat.emit_system(f"⚠️ Brain loop crashed: {error!r} — stop and start the brain to recover.")
        finally:
            heartbeat.cancel()
            if self._speaker is not None:
                self._speaker.mute()
            for task in (turn, spoke):
                if task is not None:
                    task.cancel()  # stop() can land mid-race; the turn dies with the loop

    async def _turn(self, session: GeminiSession) -> None:
        """One turn: look at the world, think with Gemini, commit, act."""
        events = list(self._events)  # a peek — consumed only if the turn commits
        self._turn_count += 1
        self._turn_started_at = time.monotonic()
        # Published before the first await, so the racing loop always reads
        # the current turn's speaker: once it has spoken, no abandoning.
        speaker = self._speaker = self._chat.stream_speech()
        try:
            text, frames = self._look(events)
            if self._frame_at_capture is None:
                return  # the feed died between the loop's freshness check and the look
            images = [jpeg for _, jpeg in frames]
            wrist_frames = [i for i, (label, _) in enumerate(frames) if label == _WRIST_LABEL]
            message = GeminiSession.user_message(text, images)
            tools = self._build_tools(events)
            directive = self._state.current_directive
            system = build_system_prompt(directive.get_prompt() if directive else None)
            if self._state.log_everything:
                self._logger.info(f"[Brain] Turn input:\n{text}")
            self._trace_turn_start(text, frames, tools, system, session)

            # The only blocking call, on a worker thread. Cancellation unwinds
            # HERE — the orphaned HTTP call finishes and its result is dropped.
            self._turn_in_flight = True
            try:
                response = await asyncio.to_thread(
                    session.generate,
                    message,
                    tools,
                    system,
                    speaker.feed if speaker else None,
                    latest_only_images=wrist_frames,
                )
            finally:
                self._turn_in_flight = False

            latency = self._elapsed()
            if self._error_streak:
                self._error_streak = 0
                self._chat.emit_system("✅ Brain recovered.")
            if not self._state.is_brain_active:
                # Deactivation raced this turn's response: drop the whole exchange.
                self._trace("turn_dropped", turn=self._turn_count, latency=latency)
                return

            decision = session.absorb(message, response, latest_only_images=wrist_frames)
            del self._events[: len(events)]  # commit: consume exactly what this turn saw
            outcomes = self._act(decision, speaker, session)
            self._trace(
                "turn_end",
                turn=self._turn_count,
                latency=latency,
                thoughts=decision.thoughts,
                speech=decision.speech,
                calls=[{"name": call.name, "args": call.args, "outcome": outcome} for call, outcome in outcomes],
                history=session.history_len,
                next_in=round(self._interval(), 1),
            )
        except asyncio.CancelledError:
            self._trace("turn_dropped", turn=self._turn_count, latency=self._elapsed())
            raise
        except Exception as error:
            # Inference failures and turn-level bugs alike: retry, never die.
            self._error_streak += 1
            self._logger.error(f"[Brain] Turn failed ({self._error_streak}x): {error!r}")
            if self._error_streak == 1:
                self._chat.emit_system(f"⚠️ Brain turn failed: {error} — retrying.")
            backoff = min(5.0 * self._error_streak, 30.0)
            self._trace(
                "turn_error", turn=self._turn_count, error=str(error), streak=self._error_streak, backoff=backoff
            )
            await self._pause(backoff, seen=len(events))  # events stay queued; a new one ends this early

    async def _await_camera(self) -> None:
        """Hold turns while the camera feed is down; tell the user if it stays down."""
        for _ in range(25):  # brief grace: the feed may just be starting up
            if self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC) is not None:
                return
            await asyncio.sleep(0.2)
        self._logger.error("[Brain] Camera feed is down; holding turns until it returns")
        self._chat.emit_system("⚠️ No camera frames — the brain is waiting for the feed to return.")
        while self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC) is None:
            del self._events[:-_MAX_EVENTS_WHILE_BLIND]  # don't hoard stimuli while blind
            await asyncio.sleep(0.2)
        self._chat.emit_system("✅ Camera feed is back.")

    async def _pause(self, seconds: float, *, seen: int = 0) -> None:
        """Sleep up to ``seconds``; the queue growing past ``seen`` events ends it early."""
        self._new_event.clear()
        if len(self._events) > seen:
            return
        self._pause_until = time.monotonic() + seconds
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._new_event.wait(), seconds)
        finally:
            self._pause_until = 0.0

    def _interval(self) -> float:
        if self._state.primitive_running:
            return self._config.supervision_turn_interval
        return self._config.idle_turn_interval

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._turn_started_at, 2)

    # ================= look =================
    def _look(self, events: list[dict]) -> tuple[str, list[tuple[str, bytes]]]:
        """Snapshot the world + the given (peeked) events into one turn input.

        Returns the input text and the turn's images as (label, jpeg) pairs —
        only the bytes go to the model; the labels feed telemetry and mark
        which frames are latest-only in history (the wrist camera).
        Frame, head pitch, and pose are captured together: go_to_point_in_view
        projections must use the geometry of the exact frame the model saw.
        """
        pose = self._pose_at_capture = self._pose.current_pose_xyt()
        status = f"[t+{int(time.monotonic() - self._activated_at)}s]"
        if pose is not None:
            status += f" pose: x={pose[0]:.2f}m y={pose[1]:.2f}m heading={math.degrees(pose[2]):.0f}°"
        running = self._state.primitive_running
        if running:
            status += f" | running skill: {running['primitive_name']}"

        lines = [status]
        if running:
            meta = self._state.registry.primitives.get(running.get("skill_id")) or {}
            guidance = (meta.get("guidelines_when_running") or "").strip()
            if guidance:
                lines.append(f"(guidance while this skill runs: {guidance})")
        lines += [f"- {event['text']}" for event in events]

        head_jpeg = self._frame_at_capture = self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC)
        self._pitch_at_capture = self._camera.current_head_pitch
        # A dead feed means the caller abandons the turn on _frame_at_capture,
        # so the frames it gets back here are moot — no None goes downstream.
        frames: list[tuple[str, bytes]] = [("head camera", head_jpeg)] if head_jpeg is not None else []
        arm_jpeg = self._camera.fresh_arm_jpeg(_FRESH_FRAME_SEC)
        if arm_jpeg is not None:
            frames.append((_WRIST_LABEL, arm_jpeg))
            lines.append("(second image is the arm wrist camera)")
        frames += [("event image", event["image"]) for event in events if event["image"]]
        return "\n".join(lines), frames

    def _build_tools(self, events: list[dict]):
        running = self._state.primitive_running
        if running is not None:
            user_spoke = any(event["kind"] == "user" for event in events)
            return build_tools([], running["primitive_name"], user_spoke=user_spoke)
        active_ids = set(self._roster.active_skill_ids())
        skills = [meta for meta in self._state.registry.metadata if meta["id"] in active_ids]
        self._tool_map = {name: meta["id"] for name, meta in assign_tool_names(skills)}
        return build_tools(skills, None, can_go_to_point_in_view=_NAV_TO_POSITION in active_ids)

    # ================= act =================
    def _act(self, decision, speaker, session: GeminiSession) -> list:
        # Execute and answer the calls before any chat I/O: a functionCall
        # left unanswered in history poisons every later request.
        outcomes = [(call, self._execute(call)) for call in decision.calls]
        session.add_tool_outcomes(outcomes)
        if decision.thoughts:
            self._chat.emit_thoughts(decision.thoughts)
        speech = decision.speech
        if speech and not speaker.spoke and any(event["kind"] == "user" for event in self._events):
            # Never started talking and the user has already said something
            # newer — voicing this reply now would talk over the conversation.
            self._logger.info(f"[Brain] Speech suppressed (newer user message pending): {speech[:60]!r}")
            speaker.mute()
        speaker.flush()  # the reply's last sentence has no trailing boundary
        if speaker.spoke and speech:
            # Audio went out sentence-by-sentence; the panel still gets the
            # full reply as one message.
            self._chat.emit("robot", speech, speak=False)
        return outcomes

    def _execute(self, call) -> str:
        """Run one tool call; the returned string is the model-facing outcome.

        Never raises: the turn has already committed, so an escaping error
        would orphan the model's function calls in history and rerun the turn
        without the events that triggered it.
        """
        self._logger.info(f"[Brain] Tool call: {call.name}({call.args})")
        try:
            return self._dispatch(call)
        except Exception as error:
            self._logger.error(f"[Brain] Tool call {call.name} failed: {error!r}")
            return f"failed — {error}"

    def _dispatch(self, call) -> str:
        if call.name == WAIT:
            return "ok"
        if call.name == STOP_SKILL:
            return self._stop_skill()
        if self._state.primitive_running is not None:
            return "rejected — another skill is already running; stop it first"
        if call.name == GO_TO_POINT_IN_VIEW:
            return self._go_to_point_in_view(call.args)

        # Only names declared this turn resolve: falling back to the full
        # registry would let a hallucinated call bypass the directive's
        # active-skill allowlist.
        skill_id = self._tool_map.get(call.name)
        if skill_id is None:
            return f"unknown skill '{call.name}'"
        self._start_skill(skill_id, self._adjust_nav_goal(skill_id, dict(call.args)))
        return "started — you will get an event when it finishes"

    def _stop_skill(self) -> str:
        if self._runner.has_active_goal:
            self._runner.cancel_active_goal()
            return "stopping — you will get an event when it has stopped"
        if self._state.primitive_running is None:
            return "no skill is running"
        # A run this client didn't start (webapp/CLI manual run): the skills
        # server's owner-agnostic cancel is the only handle.
        if self._runner.cancel_external():
            return "stopping — you will get an event when it has stopped"
        return "could not stop it — the skills server is unreachable"

    def _go_to_point_in_view(self, args: dict) -> str:
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

        inputs = self._adjust_nav_goal(nav_id, grounding.approach_goal(*floor))
        self._logger.info(
            f"[Brain] go_to_point_in_view ({v_norm:.0f},{u_norm:.0f}) -> floor ({floor[0]:.2f}, {floor[1]:.2f})m, "
            f"goal ({inputs['x']:.2f}, {inputs['y']:.2f}, {inputs['theta_degrees']:.0f}°) "
            f"pitch={self._pitch_at_capture:.0f}°"
        )
        self._start_skill(nav_id, inputs)
        return (
            f"driving to the floor point {math.hypot(*floor):.1f}m away (stopping ~{grounding.STANDOFF_M}m short) "
            "— you will get an event when it finishes"
        )

    def _start_skill(self, skill_id: str, inputs: dict) -> None:
        self._gaze.pause()
        self._runner.start_task(skill_id, f"local-{uuid.uuid4().hex[:8]}", inputs)

    def _adjust_nav_goal(self, skill_id: str, inputs: dict) -> dict:
        """Ground a nav goal in the robot's current pose before sending it."""
        if skill_id != _NAV_TO_POSITION:
            return inputs
        current = self._pose.current_pose_xyt()
        if not inputs.get("local_frame", False):
            # Mapfree has no map frame: re-base the model's absolute (odom)
            # goal onto the robot — the map planner would only reject it.
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

    # ================= events (executor thread) =================
    def add_event(self, text: str, image: bytes | None = None, kind: str = "info") -> None:
        """Queue something that happened; the loop wakes for an immediate turn."""
        if not self.available:
            return  # no transport, no loop: these would accumulate forever
        self._events.append({"text": text, "image": image, "kind": kind})
        self._runtime.post(self._wake, kind)
        self._trace("event", kind=kind, text=text, image=image is not None)

    def _wake(self, kind: str) -> None:
        """Loop thread: end any pause; user speech also abandons a housekeeping turn."""
        self._new_event.set()
        if kind == "user":
            self._user_spoke.set()

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

    # ================= telemetry =================
    def _trace(self, event: str, **fields) -> None:
        """Publish one JSON telemetry event on /brain/trace (no-op when unwired)."""
        if self._trace_sink is not None:
            self._trace_sink(json.dumps({"ev": event, "t": time.time(), **fields}))

    def _trace_turn_start(
        self, text: str, frames: list[tuple[str, bytes]], tools: list, system: str, session: GeminiSession
    ) -> None:
        if self._trace_sink is None:
            return  # don't base64 the frames for nobody
        self._trace(
            "turn_start",
            turn=self._turn_count,
            input=text,
            images=len(frames),
            tools=[d["name"] for d in tools[0]["functionDeclarations"]],
            history=session.history_len,
            history_images=session.image_turn_count,
            system=system,
            frames=[{"label": label, "jpeg": base64.b64encode(jpeg).decode()} for label, jpeg in frames],
        )

    async def _heartbeat(self) -> None:
        while True:
            self._lidar.tick()
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
            thinking_for=self._elapsed() if self._turn_in_flight else 0,
            queued=[{"kind": e["kind"], "text": e["text"][:200]} for e in list(self._events)],
            next_in=max(0.0, round(self._pause_until - time.monotonic(), 1)) if self._pause_until else 0.0,
            streak=self._error_streak,
            running=running["primitive_name"] if running else None,
            history=self._session.history_len if self._session else 0,
            uptime=round(time.monotonic() - self._activated_at, 0) if self._state.is_brain_active else 0,
            motion=round(self._camera.motion_peak(), 4),
        )
