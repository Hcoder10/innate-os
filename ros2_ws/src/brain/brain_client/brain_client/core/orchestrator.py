"""Brain orchestration: the perception loop and cloud-message reactions.

Pulls together the perception (camera, pose, map) and transport collaborators to build
and send IMAGE / POSE_IMAGE payloads, and reacts to the cloud agent's control
messages (ready-for-image, registration ack, memory positions, ws status). The
heavy data transforms live in the pure ``navigation.payload`` / ``perception``
helpers; this module is the wiring.
"""

from __future__ import annotations

import json
import time

import rclpy
from std_msgs.msg import String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.navigation import payload as navpayload
from brain_client.perception import image_codec
from brain_client.transport.messages import InternalMessage, InternalMessageType, MessageIn, MessageInType

READY_FOR_CONNECTION_COUNT = 10
READY_FOR_CONNECTION_INTERVAL_SEC = 1.0
INPUT_MANAGER_WAIT_TIMEOUT_SEC = 5.0


class Orchestrator:
    def __init__(
        self,
        node,
        state,
        config,
        *,
        ws_bridge,
        camera,
        pose_tracker,
        map_state,
        chat,
        active_inputs_pub,
        memory_positions_pub,
    ):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._ws = ws_bridge
        self._camera = camera
        self._pose = pose_tracker
        self._map = map_state
        self._chat = chat
        self._active_inputs_pub = active_inputs_pub
        self._memory_positions_pub = memory_positions_pub

        # Wired by the node after construction.
        self.lifecycle = None
        self.catalog = None

        self.pose_image_timer = None
        self._ready_for_connection_timer = None
        self._ready_for_connection_remaining = 0
        self._initial_active_inputs_timer = None
        self._initial_active_inputs_remaining = int(INPUT_MANAGER_WAIT_TIMEOUT_SEC / READY_FOR_CONNECTION_INTERVAL_SEC)
        self._ws_connected = False

    # ================= perception loop =================
    def agent_loop(self) -> None:
        if not self._state.is_brain_active:
            self._logger.debug("[BrainClient] Brain not active. Skipping agent_loop.")
            return
        if not self._state.primitives_registered:
            self._logger.info("[BrainClient] Primitives not registered. Skipping agent_loop.")
            return
        if not (self._state.ready_for_image and self._camera.has_image):
            return

        self._logger.info("[BrainClient] Sending image callback.")
        try:
            use_mapfree, pose_source = self._pose.pose_source("image_callback: ")
            if pose_source is None:
                self._logger.warn("[BrainClient] No suitable pose source. Skipping image callback.")
                return

            rgb_b64 = self._camera.latest_image_b64()
            if not rgb_b64:
                self._logger.error("Failed to encode RGB image")
                self._state.ready_for_image = False
                return

            start = time.perf_counter()
            payload = {"image_b64": rgb_b64}
            label = "Image"

            if self._config.send_video_feed:
                label = "Video"
                frames = self._camera.recent_frames(self._config.video_buffer_duration_seconds)
                if not frames:
                    self._logger.warn("[BrainClient] Video enabled but no recent frames; sending single image.")
                else:
                    video_b64 = image_codec.encode_video_b64(frames, self._config.video_fps)
                    if video_b64:
                        payload["video_b64"] = video_b64
                        payload["video_format"] = "avi_mjpeg"
                    else:
                        self._logger.warn("Video creation failed; sending single image.")

            nav_payload = self.build_nav_payload(use_mapfree, pose_source)
            if nav_payload is None:
                self._logger.warn("[BrainClient] Failed to build navigation payload. Skipping image callback.")
                return
            payload.update(nav_payload)

            arm_block = self._camera.arm_block()
            if arm_block:
                payload["additional_camera"] = arm_block

            self._state.pose_at_image_send = self._pose.current_pose_xyt()
            self._ws.send_message(MessageIn(type=MessageInType.IMAGE, payload=payload))
            self._logger.info(f"{label} processing and sending took {time.perf_counter() - start:.4f} seconds.")

            self._state.ready_for_image = False
            self._camera.clear_after_send()
        except Exception as e:
            self._logger.error(f"Error in agent_loop: {e}")
            raise

    def build_nav_payload(self, use_mapfree: bool | None = None, pose_source: tuple | None = None) -> dict | None:
        """Build the depth/map/robot_coords/camera_info payload (auto pose if needed)."""
        if pose_source is None:
            use_mapfree, pose_source = self._pose.pose_source("nav_payload: ")
            if pose_source is None:
                return None

        if use_mapfree:
            map_block = navpayload.dummy_map()
        else:
            map_block = self._map.map_block()
            if map_block is None:
                self._logger.warn(
                    f"[NavPayload] No map data. use_mapfree={use_mapfree}, nav_mode={self._pose.cur_nav_mode}"
                )
                return None

        pose_msg = pose_source[1]
        pos = pose_msg.pose.position
        theta = quaternion_to_yaw(pose_msg.pose.orientation)
        if pose_source[0] == "tf":
            frame_id = "map"
        else:
            odom = self._pose.last_odom
            frame_id = odom.header.frame_id if getattr(odom, "header", None) and odom.header.frame_id else "odom"

        camera_info = {**self._config.camera_info, "pitch_deg": self._camera.current_head_pitch}
        robot_coords = navpayload.robot_coords(
            x=pos.x,
            y=pos.y,
            z=pos.z,
            theta=theta,
            frame_id=frame_id,
            covariance=getattr(pose_msg, "covariance", None),
        )
        return navpayload.assemble(
            camera_info=camera_info,
            robot_coords=robot_coords,
            map_block=map_block,
            depth_block=self._camera.depth_block(),
        )

    # ================= pose image =================
    def start_pose_image_timer_if_ready(self) -> None:
        if self._state.ready_for_image and self._state.primitives_registered and not self._state.pose_image_started:
            self._logger.info("Starting regular pose image transmission")
            self._state.pose_image_started = True
            self.pose_image_timer = self._node.create_timer(self._config.pose_image_interval, self.pose_image)

    def pose_image(self) -> None:
        try:
            if not self._camera.has_image:
                return
            nav_mode = self._pose.cur_nav_mode
            if (nav_mode is None or nav_mode == "mapping") and not self._config.simulator_mode:
                self._logger.debug(f"Skipping pose_image; navigation mode is {nav_mode}")
                return
            try:
                transform = self._pose.tf_buffer.lookup_transform(
                    target_frame="map",
                    source_frame="base_link",
                    time=rclpy.time.Time(),
                    timeout=rclpy.time.Duration(seconds=0.5),
                )
            except Exception as tf_err:
                self._logger.debug(f"TF map->base_link not ready: {tf_err}")
                return

            image_b64 = self._camera.latest_image_b64()
            if not image_b64:
                self._logger.error("Failed to encode image for pose_image")
                return

            pos = transform.transform.translation
            theta = quaternion_to_yaw(transform.transform.rotation)
            payload = {
                "image": image_b64,
                "x": pos.x,
                "y": pos.y,
                "theta": theta,
                "camera_info": {**self._config.camera_info, "pitch_deg": self._camera.current_head_pitch},
            }
            if nav_mode == "navigation" and self._pose.last_amcl_pose:
                cov = self._pose.last_amcl_pose.pose.covariance
                payload["cov_x"], payload["cov_y"], payload["cov_yaw"] = cov[0], cov[7], cov[35]
            elif nav_mode == "mapfree":
                payload["cov_x"] = payload["cov_y"] = payload["cov_yaw"] = 1e4

            self._state.pose_at_image_send = (pos.x, pos.y, theta)
            self._ws.send_message(MessageIn(type=MessageInType.POSE_IMAGE, payload=payload))
        except Exception as e:
            self._logger.error(f"Error in pose_image: {e}")

    # ================= memory positions =================
    def handle_memory_positions(self, msg) -> None:
        try:
            self._state.memory_positions = msg.payload.get("positions", [])
            self._logger.debug(f"Received {len(self._state.memory_positions)} memory positions from cloud agent")
            self.publish_memory_positions()
        except Exception as e:
            self._logger.error(f"Error handling memory_positions: {e}")

    def publish_memory_positions(self) -> None:
        if self._state.memory_positions:
            self._memory_positions_pub.publish(String(data=json.dumps({"positions": self._state.memory_positions})))

    # ================= ws control messages =================
    def handle_ready_for_image(self, msg) -> None:
        self._logger.info("Received READY_FOR_IMAGE; setting flag.")
        self._state.ready_for_image = True
        self.start_pose_image_timer_if_ready()

    def handle_registered(self, msg) -> None:
        self._logger.info(f"Registration response: {msg.payload}")
        if not msg.payload.get("success", False):
            self._logger.error("Failed to register primitives and/or directive with server")
            return
        self._state.primitives_registered = True
        self._logger.info(
            f"Successfully registered {msg.payload.get('count', 0)} primitives and directive: "
            f"{msg.payload.get('directive_registered', False)}"
        )
        if self._config.simulator_mode and not self._state.is_brain_active and self.lifecycle is not None:
            self._logger.info("[BrainClient] Auto-activating brain in simulator mode")
            self.lifecycle.activate_for_simulator()
        self.start_pose_image_timer_if_ready()

    def on_ws_status(self, msg: String) -> None:
        try:
            status = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            return
        connected = bool(status.get("connected"))
        if connected and not self._ws_connected and self.catalog is not None:
            self._logger.info("[BrainClient] WebSocket connected; re-registering primitives.")
            self._state.primitives_registered = False
            self.catalog.register()
        self._ws_connected = connected

    def on_backend_config(self, msg: String) -> None:
        try:
            cfg = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self._logger.warn("[BrainClient] Ignoring /brain/backend_config: invalid JSON payload.")
            return
        if not isinstance(cfg, dict):
            self._logger.warn("[BrainClient] Ignoring /brain/backend_config: payload must be an object.")
            return
        new_token = str(cfg.get("service_key") or cfg.get("token") or self._state.token).strip()
        if new_token != self._state.token:
            self._state.token = new_token
            self._logger.info("[BrainClient] Mirrored backend config token update.")

    # ================= handshake broadcasts =================
    def start_ready_for_connection_broadcast(self) -> None:
        if self._ready_for_connection_timer is not None:
            self._ready_for_connection_timer.cancel()
        self._ready_for_connection_remaining = READY_FOR_CONNECTION_COUNT
        self._ready_for_connection_timer = self._node.create_timer(
            READY_FOR_CONNECTION_INTERVAL_SEC, self._send_ready_for_connection
        )
        self._send_ready_for_connection()

    def _send_ready_for_connection(self) -> None:
        if self._ready_for_connection_remaining <= 0:
            if self._ready_for_connection_timer is not None:
                self._ready_for_connection_timer.cancel()
                self._ready_for_connection_timer = None
            return
        self._ws.send_message(InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION))
        self._ready_for_connection_remaining -= 1

    def start_initial_active_inputs(self) -> None:
        self._initial_active_inputs_timer = self._node.create_timer(
            READY_FOR_CONNECTION_INTERVAL_SEC, self._try_initial_active_inputs_publish
        )

    def _try_initial_active_inputs_publish(self) -> None:
        has_peer = self._node.count_subscribers("/input_manager/active_inputs") > 0
        if has_peer or self._initial_active_inputs_remaining <= 0:
            self._active_inputs_pub.publish(String(data=json.dumps({"inputs": []})))
            self._initial_active_inputs_timer.cancel()
            self._initial_active_inputs_timer = None
            return
        self._initial_active_inputs_remaining -= 1
