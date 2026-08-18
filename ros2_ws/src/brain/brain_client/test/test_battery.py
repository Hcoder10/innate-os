# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Battery in the agent's turn context, and the idle-time out-of-battery warning."""

import sys
from types import SimpleNamespace

from brain_client.brain.utils import observation_text
from brain_client.state.battery import Battery
from brain_client.transport.chat import Sender


def test_observation_text_carries_the_battery_level():
    def observation(battery: Battery | None) -> str:
        return observation_text(
            uptime_s=10, pose=None, battery=battery, running_skill=None, guidance="", events=[], has_wrist_frame=False
        )

    assert "| battery: 47%" in observation(Battery(percentage=0.474, voltage=22.0, current=-1.0, charging=False))
    assert "battery" not in observation(None)


# ---------- the voice warning (monitor built against a faked sensor_msgs) ----------

DISCHARGING, CHARGING = 2, 1


def make_monitor(monkeypatch, brain_is_active):
    from brain_client.perception.battery import BatteryMonitor

    fake_msg = SimpleNamespace(BatteryState=SimpleNamespace(POWER_SUPPLY_STATUS_CHARGING=CHARGING))
    monkeypatch.setitem(sys.modules, "sensor_msgs", SimpleNamespace(msg=fake_msg))
    monkeypatch.setitem(sys.modules, "sensor_msgs.msg", fake_msg)
    node = SimpleNamespace(create_subscription=lambda *a, **k: None)
    chat = SimpleNamespace(emitted=[])
    chat.emit = lambda sender, text: chat.emitted.append((sender, text))
    return BatteryMonitor(node, chat, brain_is_active), chat


def msg(pct: float, status: int = DISCHARGING) -> SimpleNamespace:
    return SimpleNamespace(percentage=pct, voltage=22.0, current=-1.0, power_supply_status=status)


def test_warns_out_loud_only_while_the_brain_is_idle(monkeypatch):
    active = {"on": True}
    monitor, chat = make_monitor(monkeypatch, lambda: active["on"])

    monitor._on_battery(msg(0.01))
    assert chat.emitted == []  # agent mode: the model sees the level and speaks for itself
    assert monitor.current.percentage == 0.01

    active["on"] = False
    monitor._on_battery(msg(0.01))
    (warning,) = chat.emitted
    assert warning[0] == Sender.ROBOT  # ROBOT entries are spoken through TTS
    assert "battery" in warning[1].lower()


def test_warns_once_per_depletion_and_rearms_after_a_swap(monkeypatch):
    monitor, chat = make_monitor(monkeypatch, lambda: False)

    monitor._on_battery(msg(0.5))
    monitor._on_battery(msg(0.01, CHARGING))
    assert chat.emitted == []  # healthy or charging: no warning

    monitor._on_battery(msg(0.01))
    monitor._on_battery(msg(0.01))
    assert len(chat.emitted) == 1  # latched: one warning per depletion

    monitor._on_battery(msg(0.9))  # battery swapped
    monitor._on_battery(msg(0.01))
    assert len(chat.emitted) == 2
