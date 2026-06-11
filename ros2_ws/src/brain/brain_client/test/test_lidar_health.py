"""TEMPORARY verification tests for INN-474 (lidar disconnect detection).

To be removed after manual review — exercises ScanHealthMonitor staleness
tracking and the orchestrator's error/recovery surfacing.
"""

import time
from types import SimpleNamespace

from brain_client.core.orchestrator import Orchestrator
from brain_client.perception.scan_health import ScanHealthMonitor


class FakeLogger:
    def __init__(self):
        self.errors = []
        self.infos = []

    def error(self, msg):
        self.errors.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    def warn(self, msg):
        pass

    def debug(self, msg):
        pass


class FakeNode:
    def __init__(self):
        self.subscriptions = []
        self._logger = FakeLogger()

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions.append((msg_type, topic, callback, qos))
        return SimpleNamespace()

    def get_logger(self):
        return self._logger


class FakeChat:
    def __init__(self):
        self.system_messages = []

    def emit_system(self, text):
        self.system_messages.append(text)


# ---------- ScanHealthMonitor ----------


def test_monitor_stale_when_no_scan_ever():
    monitor = ScanHealthMonitor(FakeNode(), scan_topic="/scan", stale_after_sec=0.05)
    assert not monitor.is_stale()  # grace period right after startup
    time.sleep(0.06)
    assert monitor.is_stale()
    assert "since startup" in monitor.describe_problem()


def test_monitor_recovers_on_scan_then_goes_stale_again():
    monitor = ScanHealthMonitor(FakeNode(), scan_topic="/scan", stale_after_sec=0.05)
    time.sleep(0.06)
    assert monitor.is_stale()
    monitor._on_scan(None)
    assert not monitor.is_stale()
    time.sleep(0.06)
    assert monitor.is_stale()
    assert "stopped" in monitor.describe_problem()


def test_monitor_subscribes_to_configured_topic():
    node = FakeNode()
    ScanHealthMonitor(node, scan_topic="/my_scan")
    assert node.subscriptions[0][1] == "/my_scan"


# ---------- Orchestrator.check_lidar_health ----------


def make_orchestrator(*, stale, mapfree=False, simulator=False, scan_health="default"):
    node = FakeNode()
    if scan_health == "default":
        scan_health = SimpleNamespace(is_stale=lambda: stale, describe_problem=lambda: "no laser scan data")
    chat = FakeChat()
    orch = Orchestrator(
        node,
        SimpleNamespace(),
        SimpleNamespace(simulator_mode=simulator),
        ws_bridge=None,
        camera=None,
        pose_tracker=SimpleNamespace(is_mapfree=mapfree),
        map_state=None,
        chat=chat,
        catalog=None,
        active_inputs_pub=None,
        memory_positions_pub=None,
        scan_health=scan_health,
    )
    return orch, chat, node._logger


def test_stale_scan_emits_error_once():
    orch, chat, logger = make_orchestrator(stale=True)
    orch.check_lidar_health()
    orch.check_lidar_health()
    orch.check_lidar_health()
    assert len(chat.system_messages) == 1
    assert "LiDAR is not responding" in chat.system_messages[0]
    assert len(logger.errors) == 1


def test_recovery_emits_restored_message():
    orch, chat, _ = make_orchestrator(stale=True)
    orch.check_lidar_health()
    orch._scan_health.is_stale = lambda: False
    orch.check_lidar_health()
    orch.check_lidar_health()
    assert len(chat.system_messages) == 2
    assert "publishing again" in chat.system_messages[1]


def test_healthy_scan_emits_nothing():
    orch, chat, _ = make_orchestrator(stale=False)
    orch.check_lidar_health()
    assert chat.system_messages == []


def test_mapfree_mode_suppresses_error():
    orch, chat, _ = make_orchestrator(stale=True, mapfree=True)
    orch.check_lidar_health()
    assert chat.system_messages == []


def test_simulator_mode_suppresses_error():
    orch, chat, _ = make_orchestrator(stale=True, simulator=True)
    orch.check_lidar_health()
    assert chat.system_messages == []


def test_no_monitor_is_safe():
    orch, chat, _ = make_orchestrator(stale=True, scan_health=None)
    orch.check_lidar_health()
    assert chat.system_messages == []
