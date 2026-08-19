import math
from types import SimpleNamespace
from unittest.mock import Mock

import mars_sim_driver.grid_localizer_sim as localizer_module
from mars_sim_driver.grid_localizer_sim import GridLocalizerSim, _pose_matches


def _pose(*, x: float = 0.0, y: float = 0.0, yaw: float = 0.0):
    return SimpleNamespace(
        position=SimpleNamespace(x=x, y=y),
        orientation=SimpleNamespace(x=0.0, y=0.0, z=math.sin(yaw / 2.0), w=math.cos(yaw / 2.0)),
    )


def _odom(*, x: float = 0.0, y: float = 0.0, yaw: float = 0.0, stamp_s: float = 0.0):
    stamp_ns = round(stamp_s * 1_000_000_000)
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(
                sec=stamp_ns // 1_000_000_000,
                nanosec=stamp_ns % 1_000_000_000,
            )
        ),
        pose=SimpleNamespace(pose=_pose(x=x, y=y, yaw=yaw)),
    )


def _node(*, odom=None, pending_pose=None, timer=None):
    node = SimpleNamespace(
        _last_odom=odom,
        _pending_pose=pending_pose,
        _retry_timer=timer,
        _drift_bad_since=None,
        _drift_bad_samples=0,
        _drift_armed=True,
        _drift_healthy_since=None,
        _last_auto_reseed_at=None,
        _begin_localization=Mock(return_value=True),
        get_logger=lambda: SimpleNamespace(info=Mock(), warning=Mock()),
    )
    node._clear_drift_observation = lambda: GridLocalizerSim._clear_drift_observation(node)
    return node


def _amcl(*, x: float = 0.0, y: float = 0.0, yaw: float = 0.0):
    return SimpleNamespace(pose=SimpleNamespace(pose=_pose(x=x, y=y, yaw=yaw)))


def test_odom_pose_reset_reseeds_amcl_even_when_clock_does_not_rewind():
    node = _node(odom=_odom(x=3.0, y=4.0, stamp_s=10.0))
    node._drift_armed = False
    node._last_auto_reseed_at = 10.0

    GridLocalizerSim._on_odom(node, _odom(x=-4.3, y=-0.2, yaw=-math.pi / 2.0, stamp_s=10.1))

    node._begin_localization.assert_called_once_with()
    assert node._drift_armed is True
    assert node._last_auto_reseed_at is None


def test_normal_odom_motion_does_not_reseed_amcl():
    node = _node(odom=_odom(x=1.0, y=2.0, yaw=0.1, stamp_s=10.0))

    GridLocalizerSim._on_odom(node, _odom(x=1.03, y=2.02, yaw=0.11, stamp_s=10.1))

    node._begin_localization.assert_not_called()


def test_large_odom_change_after_callback_stall_does_not_immediately_reseed_amcl():
    node = _node(odom=_odom(x=1.0, y=2.0, yaw=0.0, stamp_s=10.0))

    GridLocalizerSim._on_odom(node, _odom(x=2.0, y=2.0, yaw=math.pi / 2.0, stamp_s=10.5))

    node._begin_localization.assert_not_called()


def test_stale_amcl_reply_does_not_end_retries():
    timer = SimpleNamespace(cancel=Mock())
    node = _node(pending_pose=(1.0, 2.0, 0.0), timer=timer)

    GridLocalizerSim._on_amcl_pose(node, _amcl(x=4.0, y=2.0))

    timer.cancel.assert_not_called()
    assert node._retry_timer is timer
    assert node._pending_pose == (1.0, 2.0, 0.0)


def test_matching_amcl_reply_ends_retries():
    timer = SimpleNamespace(cancel=Mock())
    node = _node(pending_pose=(1.0, 2.0, 0.0), timer=timer)

    GridLocalizerSim._on_amcl_pose(node, _amcl(x=1.1, y=2.1))

    timer.cancel.assert_called_once_with()
    assert node._retry_timer is None
    assert node._pending_pose is None


def test_pose_match_rejects_wrong_heading_at_same_position():
    assert not _pose_matches(_pose(x=1.0, y=2.0, yaw=1.0), (1.0, 2.0, 0.0))


def test_pending_localization_suppresses_drift_detector():
    node = _node(odom=_odom(), pending_pose=(0.0, 0.0, 0.0))

    GridLocalizerSim._on_amcl_pose(node, _amcl(x=3.0, y=0.0))

    node._begin_localization.assert_not_called()
    assert node._drift_bad_samples == 0


def test_persistent_amcl_drift_reseeds_once(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(localizer_module.time, "monotonic", lambda: now[0])
    node = _node(odom=_odom(x=-0.74, y=3.40, yaw=-1.0))

    for _ in range(localizer_module.AMCL_DRIFT_MIN_SAMPLES):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=-0.69, y=0.56, yaw=-1.0))
        now[0] += localizer_module.AMCL_DRIFT_DWELL_S / (localizer_module.AMCL_DRIFT_MIN_SAMPLES - 1)

    node._begin_localization.assert_called_once_with()
    assert node._drift_armed is False

    for _ in range(10):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=-0.69, y=0.56, yaw=-1.0))
        now[0] += 1.0
    node._begin_localization.assert_called_once_with()


def test_persistent_amcl_yaw_drift_reseeds(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(localizer_module.time, "monotonic", lambda: now[0])
    node = _node(odom=_odom(x=1.0, y=2.0, yaw=0.0))

    for _ in range(localizer_module.AMCL_DRIFT_MIN_SAMPLES):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=1.0, y=2.0, yaw=math.radians(40.0)))
        now[0] += localizer_module.AMCL_DRIFT_DWELL_S / (localizer_module.AMCL_DRIFT_MIN_SAMPLES - 1)

    node._begin_localization.assert_called_once_with()


def test_persistent_moderate_amcl_drift_reseeds_after_longer_dwell(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(localizer_module.time, "monotonic", lambda: now[0])
    node = _node(odom=_odom(x=0.0, y=0.0))
    interval = (localizer_module.AMCL_MODERATE_DRIFT_DWELL_S + 0.1) / (
        localizer_module.AMCL_MODERATE_DRIFT_MIN_SAMPLES - 1
    )

    for _ in range(localizer_module.AMCL_MODERATE_DRIFT_MIN_SAMPLES - 1):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.6, y=0.0))
        now[0] += interval
    node._begin_localization.assert_not_called()

    GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.6, y=0.0))

    node._begin_localization.assert_called_once_with()


def test_transient_moderate_amcl_drift_does_not_reseed(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(localizer_module.time, "monotonic", lambda: now[0])
    node = _node(odom=_odom(x=0.0, y=0.0))

    for _ in range(localizer_module.AMCL_MODERATE_DRIFT_MIN_SAMPLES):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.6, y=0.0))
        now[0] += 0.2
    GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.1, y=0.0))

    node._begin_localization.assert_not_called()
    assert node._drift_bad_samples == 0
    assert node._drift_bad_since is None


def test_transient_amcl_drift_is_cleared_by_a_healthy_update(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(localizer_module.time, "monotonic", lambda: now[0])
    node = _node(odom=_odom(x=0.0, y=0.0))

    for _ in range(localizer_module.AMCL_DRIFT_MIN_SAMPLES - 1):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=1.1, y=0.0))
        now[0] += 0.1
    GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.1, y=0.0))

    node._begin_localization.assert_not_called()
    assert node._drift_bad_samples == 0
    assert node._drift_bad_since is None


def test_drift_guard_rearms_only_after_health_and_cooldown(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(localizer_module.time, "monotonic", lambda: now[0])
    node = _node(odom=_odom(x=0.0, y=0.0))

    for _ in range(localizer_module.AMCL_DRIFT_MIN_SAMPLES):
        GridLocalizerSim._on_amcl_pose(node, _amcl(x=1.1, y=0.0))
        now[0] += localizer_module.AMCL_DRIFT_DWELL_S / (localizer_module.AMCL_DRIFT_MIN_SAMPLES - 1)
    assert node._drift_armed is False

    GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.1, y=0.0))
    now[0] += localizer_module.AMCL_HEALTHY_REARM_S + 0.1
    GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.1, y=0.0))
    assert node._drift_armed is False

    now[0] = node._last_auto_reseed_at + localizer_module.AMCL_RESEED_COOLDOWN_S + 0.1
    GridLocalizerSim._on_amcl_pose(node, _amcl(x=0.1, y=0.0))

    assert node._drift_armed is True
