# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from rclpy.node import Node
from std_msgs.msg import String

from brain_client.common.logging import UniversalLogger

# brain_client_node subscribes here and plays the text through the robot's
# voice (Cartesia TTS); see transport/tts.py. It reports playback on the
# status topic, which say(wait=True) watches.
TTS_TOPIC = "/brain/tts"
TTS_STATUS_TOPIC = "/tts/is_playing"


class SkillResult(Enum):
    """
    Enum representing the possible results of a skill execution.
    """

    SUCCESS = "success"  # The skill completed successfully
    FAILURE = "failure"  # The skill failed to complete
    CANCELLED = "cancelled"  # The skill was cancelled before completion


class SkillOutput(str):
    """A skill's output message, with an optional structured payload on .data.

    It IS the message string (prints, formats, compares like one), so existing
    string-shaped code is unaffected. A skill that returns a third element from
    execute() — ``(message, status, data)`` — makes that object available to
    chaining callers: ``pose = detect_board(); pose.data["x"]``.
    """

    data: Any = None

    def __new__(cls, message: str, data: Any = None):
        output = super().__new__(cls, message)
        output.data = data
        return output


def normalize_skill_result(result) -> tuple[SkillOutput, "SkillResult"]:
    """Accept execute()'s ``(message, status)`` or ``(message, status, data)``.

    Returns ``(SkillOutput, status)`` — the data (None if absent) rides on the
    message string, so every consumer keeps its two-tuple shape.
    """
    if isinstance(result, (tuple, list)) and len(result) == 3:
        message, status, data = result
        return SkillOutput(message, data), status
    message, status = result
    return SkillOutput(message), status


class RobotStateType(Enum):
    """
    Enum representing the types of robot state a skill might require.
    """

    LAST_MAIN_CAMERA_IMAGE_B64 = "last_main_camera_image_b64"
    LAST_WRIST_CAMERA_IMAGE_B64 = "last_wrist_camera_image_b64"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"
    LAST_HEAD_POSITION = "last_head_position"


class InterfaceType(Enum):
    """
    Enum representing the types of interfaces a skill might require.
    """

    MANIPULATION = "manipulation"
    MOBILITY = "mobility"
    HEAD = "head"


class RobotState:
    """
    Descriptor for declaring and accessing robot state in skills.

    Usage:
        class MySkill(Skill):
            image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
            odom = RobotState(RobotStateType.LAST_ODOM)

            def execute(self):
                if self.image:  # Access state directly
                    ...
    """

    def __init__(self, state_type: RobotStateType):
        self.state_type = state_type
        self._attr_name: str | None = None

    def __set_name__(self, owner: type, name: str):
        """Called when the descriptor is assigned to a class attribute."""
        self._attr_name = f"_robot_state_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        """Get the current state value."""
        if obj is None:
            return self
        return getattr(obj, self._attr_name, None)

    def __set__(self, obj: Any, value: Any):
        """Set the state value."""
        setattr(obj, self._attr_name, value)


class Interface:
    """
    Descriptor for declaring and accessing interfaces in skills.

    Usage:
        class MySkill(Skill):
            mobility = Interface(InterfaceType.MOBILITY)
            head = Interface(InterfaceType.HEAD)

            def execute(self):
                self.mobility.rotate(0.5)  # Use interface directly
    """

    def __init__(self, interface_type: InterfaceType):
        self.interface_type = interface_type
        self._attr_name: str | None = None

    def __set_name__(self, owner: type, name: str):
        """Called when the descriptor is assigned to a class attribute."""
        self._attr_name = f"_interface_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        """Get the interface instance."""
        if obj is None:
            return self
        return getattr(obj, self._attr_name, None)

    def __set__(self, obj: Any, value: Any):
        """Set the interface instance."""
        setattr(obj, self._attr_name, value)


class Skill(ABC):
    # Stamped by the loader to "shipped" or "user" based on origin directory.
    source: str = "user"

    def __init__(self, logger):
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.node: Node | None = None
        self._feedback_callback = None
        # Lets this skill run other skills, in order, from inside execute().
        # The friendly form is function proxies (see innate/skills.py):
        #   from innate.skills import navigate_to_position
        #   navigate_to_position(x=1.0, y=0.0, theta=0.0)
        # The explicit form, for dynamic ids, is:
        #   self.skills.run("local/my_grasp_policy")   # learned/replay too
        # Each child runs to completion before the next and shows up as its own
        # step in the app. The skills server injects this before execute().
        self.skills = None
        self._say_publisher = None  # lazy, see say()
        self._tts_status_sub = None  # lazy, see say(wait=True)
        self._tts_playing = None  # last /tts/is_playing value ("true"/"false")

    @property
    @abstractmethod
    def name(self):
        """
        The name of the skill.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def execute(self, *args, **kwargs):
        """
        Execute the skill.

        Subclasses must implement this method.
        Returns a tuple of (result_message, result_status) where result_status
        is a SkillResult enum value. Optionally return a third element — any
        structured payload — which chaining callers receive as ``.data`` on
        the returned message (see SkillOutput).
        """
        pass

    def cancel(self):
        """
        Cancel the execution of the skill.

        This method should be safe to call at any time, even if the skill
        is not currently executing. Returns a message describing the
        cancellation result.

        The default stops whatever child skill is currently running via
        self.skills, which is all a skill that only chains others needs.
        Override it to stop work of your own (motion, loops, timers) — and if
        you also chain children, call self.skills.cancel() too.
        """
        if self.skills is not None:
            return self.skills.cancel()
        return "Nothing to cancel"

    def say(self, text: str, wait: bool = False) -> None:
        """
        Speak text through the robot's voice.

        Fire-and-forget by default: returns immediately, speech overlaps
        whatever the skill does next. With ``wait=True`` it blocks until the
        utterance has finished playing (best effort: it watches the TTS
        playback status, so if several say() calls are already queued it may
        return when an earlier one ends — use wait consistently for precise
        pacing). If speech isn't available (no audio node running, or in
        tests without a ROS node), it's a no-op either way.
        """
        if not text or self.node is None:
            return
        if self._say_publisher is None:
            self._say_publisher = self.node.create_publisher(String, TTS_TOPIC, 10)
        if wait and self._tts_status_sub is None:
            self._tts_status_sub = self.node.create_subscription(String, TTS_STATUS_TOPIC, self._on_tts_status, 10)
        self._say_publisher.publish(String(data=text))
        if wait:
            self._wait_for_speech_end(text)

    def _on_tts_status(self, msg: String) -> None:
        self._tts_playing = msg.data

    def _wait_for_speech_end(self, text: str) -> None:
        # Phase 1: wait for playback to start (TTS latency + anything queued
        # ahead of us). If it never does — TTS off, muted — don't hang the skill.
        deadline = time.time() + 15.0
        while self._tts_playing != "true":
            if time.time() > deadline:
                return
            time.sleep(0.05)
        # Phase 2: wait for it to finish; budget scales with utterance length.
        deadline = time.time() + max(30.0, 0.1 * len(text))
        while self._tts_playing == "true" and time.time() < deadline:
            time.sleep(0.05)

    def update_robot_state(self, **kwargs):
        """
        Update the skill with the latest robot state.
        Automatically populates RobotState descriptors defined on the class.
        Subclasses can override this to add custom handling.
        """
        # Auto-populate RobotState descriptors
        for name, descriptor in self._get_robot_state_descriptors().items():
            state_key = descriptor.state_type.value
            if state_key in kwargs:
                setattr(self, name, kwargs[state_key])

    def clear_robot_state(self):
        """
        Reset all RobotState descriptors to None.

        Skill instances are singletons, so values injected during a previous
        run would otherwise linger and read as fresh sensor data on the next
        one. The skills server calls this before each run.
        """
        for name in self._get_robot_state_descriptors():
            setattr(self, name, None)

    def get_required_robot_states(self) -> list[RobotStateType]:
        """
        Declare the robot states required by this skill.
        Automatically collects from RobotState descriptors defined on the class.
        """
        return [desc.state_type for desc in self._get_robot_state_descriptors().values()]

    def _get_robot_state_descriptors(self) -> dict[str, "RobotState"]:
        """Collect all RobotState descriptors from the class."""
        descriptors = {}
        for cls in type(self).__mro__:
            for name, attr in vars(cls).items():
                if isinstance(attr, RobotState) and name not in descriptors:
                    descriptors[name] = attr
        return descriptors

    def get_required_interfaces(self) -> list[InterfaceType]:
        """
        Declare the interfaces required by this skill.
        Automatically collects from Interface descriptors defined on the class.
        """
        return [desc.interface_type for desc in self._get_interface_descriptors().values()]

    def _get_interface_descriptors(self) -> dict[str, "Interface"]:
        """Collect all Interface descriptors from the class."""
        descriptors = {}
        for cls in type(self).__mro__:
            for name, attr in vars(cls).items():
                if isinstance(attr, Interface) and name not in descriptors:
                    descriptors[name] = attr
        return descriptors

    def inject_interface(self, interface_type: InterfaceType, interface_instance):
        """Inject an interface instance into the skill."""
        for name, descriptor in self._get_interface_descriptors().items():
            if descriptor.interface_type == interface_type:
                setattr(self, name, interface_instance)
                return True
        return False

    def guidelines(self):
        """
        Optionally provide guidelines for this skill.
        Subclasses may override this method if guidelines are available.
        """
        return None

    def guidelines_when_running(self):
        """
        Optionally provide guidelines for this skill when it is running.
        Subclasses may override this method if guidelines are available.
        """
        return None

    def set_feedback_callback(self, callback):
        """Sets the feedback callback function."""
        self._feedback_callback = callback
        self.logger.debug(f"Feedback callback set for skill {self.name}.")

    def _send_feedback(self, message: str, image_b64: str = None):
        """Sends feedback if the callback is set, optionally with an image."""
        self.logger.info(f"Skill feedback [{self.name}]: {message}")
        if self._feedback_callback:
            try:
                self._feedback_callback(message, image_b64)
            except Exception as e:
                self.logger.error(f"Error sending feedback for skill {self.name}: {e}")
