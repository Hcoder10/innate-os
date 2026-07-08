# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any

from rclpy.node import Node
from std_msgs.msg import String

from brain_client.common.logging import UniversalLogger

# brain_client_node plays text published here (see transport/tts.py) and
# reports playback on the status topic, which say(wait=True) watches.
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
    """A skill's output message (a plain str), with an optional structured
    payload on .data — the third element of an execute() return, if any."""

    data: Any = None

    def __new__(cls, message: str, data: Any = None):
        output = super().__new__(cls, message)
        output.data = data
        return output


def normalize_skill_result(result) -> tuple[SkillOutput, "SkillResult"]:
    """Turn execute()'s (message, status[, data]) into (SkillOutput, status)."""
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
    LAST_JOINT_STATES = "last_joint_states"
    LAST_BATTERY = "last_battery"


class InterfaceType(Enum):
    """
    Enum representing the types of interfaces a skill might require.
    """

    MANIPULATION = "manipulation"
    MOBILITY = "mobility"
    HEAD = "head"


class SkillStorage:
    """Persistent per-skill key-value store: a JSON file with dict access.

    Values must be JSON-serializable. Loaded lazily; writes are atomic
    (tmp file + os.replace).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._load()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._load()[key] = value
        self._save()

    def __delitem__(self, key: str) -> None:
        del self._load()[key]
        self._save()

    def __contains__(self, key: str) -> bool:
        return key in self._load()


def _storage_dir() -> Path:
    # same INNATE_OS_ROOT resolution as common/script_paths.py
    root = Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))
    return root / "workspace" / "skill_storage"


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
        self._cancel_latch()
        # SkillInvoker for running other skills from execute(); injected by the
        # skills server before each run (see invoker.py and innate/skills.py).
        self.skills = None
        self._say_publisher = None
        self._tts_status_sub = None
        self._tts_playing = None  # last /tts/is_playing value ("true"/"false")
        self._storage = None

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
        Returns (result_message, result_status) where result_status is a
        SkillResult enum value; an optional third element is a structured
        payload chaining callers receive as ``.data`` (see SkillOutput).
        """
        pass

    def _cancel_latch(self) -> threading.Event:
        """The cancel event, created lazily — some skills skip super().__init__()."""
        latch = self.__dict__.get("_cancel_event")
        if latch is None:
            latch = self.__dict__.setdefault("_cancel_event", threading.Event())
        return latch

    @property
    def _cancelled(self) -> bool:
        """Whether cancellation was requested for the current run.

        Latches True and ignores False: skills reset the flag at execute()
        entry, which would wipe a cancel that raced goal startup. Only the
        server re-arms it between runs (_begin_run).
        """
        return self._cancel_latch().is_set()

    @_cancelled.setter
    def _cancelled(self, value: bool):
        if value:
            self._cancel_latch().set()

    def _begin_run(self, goal_handle=None):
        """Server hook: re-arm the latch for a fresh run, recovering a cancel
        that already landed from the goal's persistent cancel status."""
        latch = self._cancel_latch()
        latch.clear()
        try:
            if goal_handle is not None and goal_handle.is_cancel_requested:
                latch.set()
        except Exception:
            pass  # duck-typed handles without cancel status

    def cancel(self):
        """
        Cancel the execution of the skill. Safe to call at any time; returns
        a message describing the result.

        The default latches self._cancelled and stops the child skill running
        via self.skills. Override it to stop work of your own — and if you
        also chain children, call self.skills.cancel() too.
        """
        self._cancel_latch().set()
        if getattr(self, "skills", None) is not None:
            return self.skills.cancel()
        return "Cancellation requested"

    def shutdown(self):  # noqa: B027
        """Release resources this instance owns. Called when the server retires
        it — reloads replace instances, and a retired one is never used again.

        Override to destroy ROS nodes/entities the skill itself created (e.g.
        Nav2 BasicNavigator nodes): a dropped instance is cyclic garbage whose
        graph entities otherwise linger until an eventual gen-2 GC pass, so
        every reload leaks subscriptions and memory. Leave entities created on
        the shared server node alone — destroying entities under a spinning
        executor is unsafe (see #497).
        """

    @property
    def storage(self) -> SkillStorage:
        """Persistent per-skill key-value store (survives restarts), backed by
        workspace/skill_storage/<skill_name>.json."""
        if self._storage is None:
            self._storage = SkillStorage(_storage_dir() / f"{self.name}.json")
        return self._storage

    def say(self, text: str, wait: bool = False) -> None:
        """
        Speak text through the robot's voice. Fire-and-forget by default;
        with ``wait=True`` it blocks until playback ends (best effort — it
        watches the TTS playback status). No-op if speech isn't available.
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
        # wait for playback to start; if it never does (TTS off, muted)
        # don't hang the skill
        deadline = time.time() + 15.0
        while self._tts_playing != "true":
            if time.time() > deadline:
                return
            time.sleep(0.05)
        # then wait for it to finish; budget scales with utterance length
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

        Skill instances are singletons, so values from a previous run would
        otherwise read as fresh sensor data on the next one. The skills
        server calls this before each run.
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
