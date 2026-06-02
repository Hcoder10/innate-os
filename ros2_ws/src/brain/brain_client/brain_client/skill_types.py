from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from rclpy.node import Node

from brain_client.logging_config import UniversalLogger


class SkillResult(Enum):
    """
    Enum representing the possible results of a skill execution.
    """

    SUCCESS = "success"  # The skill completed successfully
    FAILURE = "failure"  # The skill failed to complete
    CANCELLED = "cancelled"  # The skill was cancelled before completion


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
        is a SkillResult enum value.
        """
        pass

    @abstractmethod
    def cancel(self):
        """
        Cancel the execution of the skill.

        Subclasses must implement this method to properly handle cancellation.
        This method should be safe to call at any time, even if the skill
        is not currently executing.

        Returns a message describing the cancellation result.
        """
        pass

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
