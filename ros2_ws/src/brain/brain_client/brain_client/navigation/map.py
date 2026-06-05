"""Occupancy-grid map state.

Owns the latched ``/map`` subscription and converts the latest ``OccupancyGrid``
into the agent's map payload block via the pure :mod:`navigation.payload` helpers.
"""

from __future__ import annotations

from nav_msgs.msg import OccupancyGrid
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.navigation import payload


class MapState:
    def __init__(self, node, map_topic: str):
        self._logger = node.get_logger()
        self.last_map: OccupancyGrid | None = None
        # AMCL/map server publish with TRANSIENT_LOCAL durability; match it so we
        # receive the latched map even if we subscribe after the publisher starts.
        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._sub = node.create_subscription(OccupancyGrid, map_topic, self._on_map, qos)

    def _on_map(self, msg: OccupancyGrid) -> None:
        self.last_map = msg

    @property
    def available(self) -> bool:
        return self.last_map is not None

    def map_block(self) -> dict | None:
        """Encode the latest map, or None if no map has been received."""
        if self.last_map is None:
            return None
        info = self.last_map.info
        return payload.encode_map(
            resolution=info.resolution,
            width=info.width,
            height=info.height,
            origin_x=info.origin.position.x,
            origin_y=info.origin.position.y,
            origin_z=info.origin.position.z,
            origin_yaw=quaternion_to_yaw(info.origin.orientation),
            frame_id=self.last_map.header.frame_id,
            data=self.last_map.data,
        )
