# src/shared_queues.py

from collections import deque
from typing import NamedTuple, Optional, List, Tuple, Dict, Any
import threading
import queue
import time

from src.runtime_logging import normalize_sim_log_mode


class ChatMessage(NamedTuple):
    sender: str
    text: str
    timestamp: float
    timestamp_put_in_queue: Optional[float]  # Used to check if the message was lost
    task_status: Optional[str] = None
    primitive_id: Optional[str] = None
    skill_id: Optional[str] = None
    failure_reason: Optional[str] = None


class ChatSignal(NamedTuple):
    signal: str
    timestamp: float


class AgentInfo(NamedTuple):
    """Information about an available agent/directive."""

    id: str
    display_name: str
    display_icon: Optional[str]  # Base64-encoded icon data or None
    prompt: str
    skills: List[str]


class SkillInfo(NamedTuple):
    """Information about an available skill/primitive."""

    id: str
    name: str
    type: str
    guidelines: str
    guidelines_when_running: str
    inputs: Dict[str, Any]
    in_training: bool
    episode_count: int
    directory: str


class SharedQueues:
    """
    Minimal message broker:
    - sim_to_agent: images (and optionally robot poses)
    - agent_to_sim: control commands
    - sim_to_web: dictionary of named images for web streaming
    """

    def __init__(self, log_everything=False, sim_log_mode: str = "debug"):
        self.sim_to_agent = queue.Queue(maxsize=100)  # Commands, nav status, etc.
        self.agent_to_sim = queue.Queue(maxsize=100)
        self.sim_to_web = queue.Queue(
            maxsize=100
        )  # Will now contain dicts of named images
        self.latest_frames = {}
        self.exit_event = threading.Event()

        # Separate size-1 queue for camera/sensor data - always keeps only latest frame
        # This prevents image data from backing up and causing latency
        self.sensor_to_agent = queue.Queue(maxsize=1)
        self.latest_clock_msg: Optional[Dict[str, Any]] = None
        self.latest_arm_state_msg: Optional[Any] = None
        self.arm_torque_enabled: bool = True
        self.arm_torque_lock = threading.Lock()
        self.latest_nav_feedback_msg: Optional[Any] = None
        self.latest_agent_update_lock = threading.Lock()

        # Flag to indicate if all model outputs should be logged
        self.log_everything = log_everything
        self._sim_log_mode = normalize_sim_log_mode(sim_log_mode)
        self.sim_log_mode_lock = threading.Lock()

        # Queues specifically for chat messages
        self.chat_to_bridge = queue.Queue(maxsize=5000)
        self.chat_from_bridge = queue.Queue(maxsize=200)
        self.chat_from_bridge_lock = threading.Lock()
        self._recent_chat_from_bridge_keys: deque[tuple[str, str, float]] = deque(
            maxlen=300
        )
        self._recent_chat_from_bridge_key_set: set[tuple[str, str, float]] = set()

        # Store the latest robot position and orientation for direct access
        # Format: [x, y, z]
        self.latest_robot_position: List[float] = [0.0, 0.0, 0.0]
        # Format: [ox, oy, oz, ow] (quaternion)
        self.latest_robot_orientation: List[float] = [0.0, 0.0, 0.0, 1.0]
        self.robot_position_timestamp: float = 0.0
        self.robot_position_lock = threading.Lock()  # For thread-safe updates

        # Store available agents/directives from the robot
        self.available_agents: List[AgentInfo] = []
        self.available_skills: List[SkillInfo] = []
        self.current_agent_id: Optional[str] = None
        self.startup_agent_id: Optional[str] = None
        self.active_skill_ids: List[str] = []
        self.brain_active: bool = False
        self.available_agents_updated_at: float = 0.0
        self.agents_lock = threading.Lock()  # For thread-safe updates

        # Store the brain client's cloud/local-agent websocket state for the UI.
        self.brain_backend_status: Dict[str, Any] = {
            "state": "unknown",
            "connected": False,
            "message": "Waiting for brain backend status",
            "updated_at": None,
            "uri": None,
            "hosted": None,
        }
        self.brain_backend_status_lock = threading.Lock()

        # One-shot status map for /set_environment request/response.
        # Key: request_id, Value: {"success": bool, "error": Optional[str]}
        self.environment_apply_results: Dict[str, Dict[str, Any]] = {}
        self.environment_apply_lock = threading.Lock()

        # Lightweight runtime metrics for the local stack dashboard.
        self.camera_frame_times: Dict[str, deque[float]] = {}
        self.camera_metrics_lock = threading.Lock()

    def set_sim_log_mode(self, mode: str) -> str:
        with self.sim_log_mode_lock:
            self._sim_log_mode = normalize_sim_log_mode(mode)
            return self._sim_log_mode

    def get_sim_log_mode(self) -> str:
        with self.sim_log_mode_lock:
            return self._sim_log_mode

    def update_robot_position(
        self, x: float, y: float, z: float, timestamp: float = None
    ):
        """Thread-safe method to update the robot's position"""
        if timestamp is None:
            import time

            timestamp = time.time()

        with self.robot_position_lock:
            self.latest_robot_position = [x, y, z]
            self.robot_position_timestamp = timestamp

    def update_robot_pose(
        self,
        x: float,
        y: float,
        z: float,
        ox: float,
        oy: float,
        oz: float,
        ow: float,
        timestamp: float = None,
    ):
        """Thread-safe method to update the robot's position and orientation"""
        if timestamp is None:
            import time

            timestamp = time.time()

        with self.robot_position_lock:
            self.latest_robot_position = [x, y, z]
            self.latest_robot_orientation = [ox, oy, oz, ow]
            self.robot_position_timestamp = timestamp

    def get_robot_position(self) -> Tuple[List[float], float]:
        """Thread-safe method to get the robot's position and timestamp"""
        with self.robot_position_lock:
            return self.latest_robot_position.copy(), self.robot_position_timestamp

    def get_robot_pose(self) -> Tuple[List[float], List[float], float]:
        """Thread-safe method to get the robot's position, orientation and timestamp"""
        with self.robot_position_lock:
            return (
                self.latest_robot_position.copy(),
                self.latest_robot_orientation.copy(),
                self.robot_position_timestamp,
            )

    def update_available_agents(
        self,
        agents: List[AgentInfo],
        current_agent_id: Optional[str] = None,
        startup_agent_id: Optional[str] = None,
        active_skill_ids: Optional[List[str]] = None,
    ):
        """Thread-safe method to update available agents from the robot"""
        with self.agents_lock:
            previous_agent_id = self.current_agent_id
            self.available_agents = agents
            self.current_agent_id = current_agent_id
            self.startup_agent_id = startup_agent_id
            if active_skill_ids is not None:
                self.active_skill_ids = active_skill_ids
            elif current_agent_id and (
                current_agent_id != previous_agent_id or not self.active_skill_ids
            ):
                current_agent = next(
                    (agent for agent in agents if agent.id == current_agent_id),
                    None,
                )
                self.active_skill_ids = current_agent.skills.copy() if current_agent else []
            self.available_agents_updated_at = time.time()

    def update_available_skills(
        self,
        skills: List[SkillInfo],
        active_skill_ids: Optional[List[str]] = None,
    ):
        """Thread-safe method to update available skills from the robot."""
        with self.agents_lock:
            self.available_skills = skills
            if active_skill_ids is not None:
                self.active_skill_ids = active_skill_ids
            self.available_agents_updated_at = time.time()

    def set_current_agent(self, agent_id: str):
        """Track the selected agent locally for API responses."""
        with self.agents_lock:
            self.current_agent_id = agent_id
            current_agent = next(
                (agent for agent in self.available_agents if agent.id == agent_id),
                None,
            )
            self.active_skill_ids = current_agent.skills.copy() if current_agent else []
            self.available_agents_updated_at = time.time()

    def update_active_skill_ids(self, skill_ids: List[str]):
        """Track the active skill subset locally for API responses."""
        with self.agents_lock:
            self.active_skill_ids = skill_ids.copy()
            self.available_agents_updated_at = time.time()

    def set_brain_active(self, active: bool):
        """Track whether the brain is currently accepting user input."""
        with self.agents_lock:
            self.brain_active = active
            self.available_agents_updated_at = time.time()

    def get_available_agents(
        self,
    ) -> Tuple[
        List[AgentInfo],
        List[SkillInfo],
        Optional[str],
        Optional[str],
        List[str],
        bool,
    ]:
        """Thread-safe method to get available agents, skills, and active IDs."""
        with self.agents_lock:
            return (
                self.available_agents.copy(),
                self.available_skills.copy(),
                self.current_agent_id,
                self.startup_agent_id,
                self.active_skill_ids.copy(),
                self.brain_active,
            )

    def get_available_agents_updated_at(self) -> float:
        """Thread-safe method to get the latest available-agents update time."""
        with self.agents_lock:
            return self.available_agents_updated_at

    def set_latest_clock_msg(self, msg: Dict[str, Any]) -> None:
        with self.latest_agent_update_lock:
            self.latest_clock_msg = msg

    def set_latest_arm_state_msg(self, msg: Any) -> None:
        with self.latest_agent_update_lock:
            self.latest_arm_state_msg = msg

    def set_latest_nav_feedback_msg(self, msg: Any) -> None:
        with self.latest_agent_update_lock:
            self.latest_nav_feedback_msg = msg

    def pop_latest_agent_updates(self) -> Tuple[
        Optional[Dict[str, Any]], Optional[Any], Optional[Any]
    ]:
        with self.latest_agent_update_lock:
            clock_msg = self.latest_clock_msg
            arm_state_msg = self.latest_arm_state_msg
            nav_feedback_msg = self.latest_nav_feedback_msg
            self.latest_clock_msg = None
            self.latest_arm_state_msg = None
            self.latest_nav_feedback_msg = None
        return clock_msg, arm_state_msg, nav_feedback_msg

    def enqueue_chat_from_bridge(self, msg: ChatMessage) -> bool:
        timestamp = float(msg.timestamp or 0.0)
        key = (msg.sender, msg.text, round(timestamp, 3))

        with self.chat_from_bridge_lock:
            if key in self._recent_chat_from_bridge_key_set:
                return False

            while self.chat_from_bridge.full():
                try:
                    dropped = self.chat_from_bridge.get_nowait()
                except queue.Empty:
                    break
                dropped_timestamp = float(dropped.timestamp or 0.0)
                dropped_key = (
                    dropped.sender,
                    dropped.text,
                    round(dropped_timestamp, 3),
                )
                self._recent_chat_from_bridge_key_set.discard(dropped_key)

            try:
                self.chat_from_bridge.put_nowait(msg)
            except queue.Full:
                return False

            if (
                self._recent_chat_from_bridge_keys.maxlen is not None
                and len(self._recent_chat_from_bridge_keys)
                >= self._recent_chat_from_bridge_keys.maxlen
            ):
                old_key = self._recent_chat_from_bridge_keys.popleft()
                self._recent_chat_from_bridge_key_set.discard(old_key)
            self._recent_chat_from_bridge_keys.append(key)
            self._recent_chat_from_bridge_key_set.add(key)
            return True

    def update_brain_backend_status(self, status: Dict[str, Any]):
        """Thread-safe method to update brain/cloud backend connection status."""
        now = time.time()
        with self.brain_backend_status_lock:
            self.brain_backend_status.update(
                {
                    "state": str(status.get("state", "unknown") or "unknown"),
                    "connected": bool(status.get("connected", False)),
                    "message": status.get("message") or "",
                    "updated_at": now,
                    "uri": status.get("uri"),
                    "hosted": status.get("hosted"),
                    "timestamp": status.get("timestamp"),
                }
            )

    def get_brain_backend_status(self) -> Dict[str, Any]:
        """Thread-safe method to get brain/cloud backend connection status."""
        with self.brain_backend_status_lock:
            return dict(self.brain_backend_status)

    def set_environment_apply_result(
        self, request_id: Optional[str], success: bool, error: Optional[str] = None
    ) -> None:
        """Store result for a set_environment request ID."""
        if not request_id:
            return
        with self.environment_apply_lock:
            self.environment_apply_results[request_id] = {
                "success": success,
                "error": error,
            }

    def pop_environment_apply_result(
        self, request_id: str
    ) -> Optional[Dict[str, Any]]:
        """Pop and return a set_environment result if available."""
        with self.environment_apply_lock:
            return self.environment_apply_results.pop(request_id, None)

    def record_web_frames(self, camera_names: List[str], timestamp: Optional[float] = None):
        """Record frame timestamps for dashboard-friendly FPS estimates."""
        if timestamp is None:
            timestamp = time.time()

        with self.camera_metrics_lock:
            for camera_name in camera_names:
                history = self.camera_frame_times.get(camera_name)
                if history is None:
                    history = deque(maxlen=120)
                    self.camera_frame_times[camera_name] = history
                history.append(timestamp)

    def get_runtime_metrics(self) -> Dict[str, Any]:
        """Return lightweight runtime metrics for local stack dashboards."""
        now = time.time()
        with self.camera_metrics_lock:
            fps_by_camera: Dict[str, float] = {}
            latest_frame_age_by_camera: Dict[str, Optional[float]] = {}
            for camera_name, history in self.camera_frame_times.items():
                timestamps = list(history)
                if len(timestamps) >= 2 and timestamps[-1] > timestamps[0]:
                    fps_by_camera[camera_name] = round(
                        (len(timestamps) - 1) / (timestamps[-1] - timestamps[0]), 2
                    )
                else:
                    fps_by_camera[camera_name] = 0.0
                latest_frame_age_by_camera[camera_name] = round(
                    now - timestamps[-1], 3
                ) if timestamps else None

        with self.agents_lock:
            available_agent_count = len(self.available_agents)
            available_agents_updated_at = self.available_agents_updated_at

        with self.latest_agent_update_lock:
            latest_agent_updates = {
                "clock": self.latest_clock_msg is not None,
                "arm_state": self.latest_arm_state_msg is not None,
                "nav_feedback": self.latest_nav_feedback_msg is not None,
            }

        return {
            "queue_sizes": {
                "sim_to_agent": self.sim_to_agent.qsize(),
                "agent_to_sim": self.agent_to_sim.qsize(),
                "sim_to_web": self.sim_to_web.qsize(),
                "sensor_to_agent": self.sensor_to_agent.qsize(),
                "chat_to_bridge": self.chat_to_bridge.qsize(),
                "chat_from_bridge": self.chat_from_bridge.qsize(),
            },
            "latest_agent_updates": latest_agent_updates,
            "fps_by_camera": fps_by_camera,
            "latest_frame_age_by_camera": latest_frame_age_by_camera,
            "available_agent_count": available_agent_count,
            "available_agents_updated_at": available_agents_updated_at,
            "sim_log_mode": self.get_sim_log_mode(),
        }
