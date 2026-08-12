# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the boot sentinel concludes from a journal tail, and from two verdicts.

The number INN-769 exists to produce is a per-cause breakdown, so a classifier
that quietly calls everything "power_loss" would still look like it worked
while making the storage decision on noise. The cases worth pinning are the
ones where the two mechanisms disagree, and the ones where absence of evidence
must not read as evidence of a clean stop.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("boot_sentinel", REPO_ROOT / "scripts" / "boot_sentinel.py")
assert _spec and _spec.loader
boot_sentinel = importlib.util.module_from_spec(_spec)
sys.modules["boot_sentinel"] = boot_sentinel
_spec.loader.exec_module(boot_sentinel)


CLEAN_TAIL = """
ros-app.service: Deactivated successfully.
Stopped Innate ROS application.
Reached target Shutdown.
systemd-shutdown[1]: Syncing filesystems and block devices.
systemd-shutdown[1]: Powering off.
"""

PANIC_TAIL = """
mars_arm: torque enabled
Unable to handle kernel NULL pointer dereference at virtual address 0000000000000018
Internal error: Oops: 96000004 [#1] PREEMPT SMP
Kernel panic - not syncing: Attempted to kill init!
"""

# A robot that ran fine and then simply stopped logging: no complaint, no
# shutdown sequence. This is what a surprise cut looks like from the disk.
TRUNCATED_TAIL = """
logger_node: vitals: cpu 44% | mem 53%
mars_nav: goal reached
logger_node: vitals: cpu 41% | mem 53%
"""


def test_a_shutdown_sequence_is_the_only_thing_that_reads_as_clean():
    assert boot_sentinel.classify_journal(CLEAN_TAIL) == ("clean", [])


def test_a_log_that_just_stops_is_truncated_not_clean():
    verdict, evidence = boot_sentinel.classify_journal(TRUNCATED_TAIL)
    assert verdict == "truncated"
    assert evidence == []


def test_a_panic_is_named_and_carries_its_own_evidence():
    verdict, evidence = boot_sentinel.classify_journal(PANIC_TAIL)
    assert verdict == "kernel_panic"
    assert any("Kernel panic" in line for line in evidence)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("watchdog: BUG: soft lockup - CPU#3 stuck for 26s!", "watchdog"),
        ("tegra_wdt tegra_wdt: last reset is due to watchdog timeout", "watchdog"),
        ("pcieport 0001:00:00.0: Link Down", "pcie_link_down"),
        ("nvme nvme0: controller is down; will reset", "pcie_link_down"),
        ("vdd-3v3-sys: under-voltage detected", "brownout"),
    ],
)
def test_each_cause_has_its_own_marker(line, expected):
    assert boot_sentinel.classify_journal(line)[0] == expected


def test_an_ethernet_unplug_is_not_a_pcie_link_down():
    """`Link Down` alone is what every NIC logs on unplug — far too broad to use."""
    tail = "eth0: Link is Down\nigb 0000:01:00.0: eth0: PCIe Link Speed unchanged"
    assert boot_sentinel.classify_journal(tail)[0] == "truncated"


def test_a_panic_outranks_a_lockup_logged_alongside_it():
    """Ordered patterns, so the specific cause wins over the general symptom."""
    tail = "watchdog: BUG: soft lockup - CPU#0 stuck\nKernel panic - not syncing: hung"
    assert boot_sentinel.classify_journal(tail)[0] == "kernel_panic"


# ── resolve_cause: where the two mechanisms meet ────────────────────


def test_the_flag_is_authoritative_for_graceful():
    """ExecStop demonstrably ran, so the shutdown path was walked regardless."""
    assert boot_sentinel.resolve_cause("graceful", "truncated") == "clean"
    assert boot_sentinel.resolve_cause("graceful", "no_prior_boot") == "clean"


def test_a_dirty_flag_against_a_quiet_journal_is_power_loss():
    """The brownout / SYS_RESET signature: userspace never learned anything was wrong."""
    assert boot_sentinel.resolve_cause("ungraceful", "truncated") == "power_loss"


def test_the_journal_names_the_cause_once_the_flag_says_something_went_wrong():
    assert boot_sentinel.resolve_cause("ungraceful", "kernel_panic") == "kernel_panic"
    assert boot_sentinel.resolve_cause("ungraceful", "pcie_link_down") == "pcie_link_down"


def test_the_first_boot_after_install_admits_it_does_not_know():
    """Guessing "clean" here would bias the one number this exists to produce."""
    assert boot_sentinel.resolve_cause("unknown", "no_prior_boot") == "unknown"
    assert boot_sentinel.resolve_cause("unknown", "truncated") == "unknown"


def test_a_crash_dump_still_names_the_cause_with_no_flag_to_go_on():
    """pstore survives the reset that cost the journal its tail, and the flag its write."""
    assert boot_sentinel.resolve_cause("unknown", "kernel_panic") == "kernel_panic"


# ── on_boot: the state machine, against a scratch state dir ─────────

OLD_DUMP = ("dmesg-ramoops-0:1700000000", "Kernel panic - not syncing: a crash from before we were here")
NEW_DUMP = ("dmesg-ramoops-1:1800000000", "Kernel panic - not syncing: Attempted to kill init!")


@pytest.fixture
def sentinel(tmp_path, monkeypatch):
    """The sentinel pointed at a scratch state dir, with pstore under our control."""
    monkeypatch.setattr(boot_sentinel, "STATE_DIR", tmp_path)
    monkeypatch.setattr(boot_sentinel, "FLAG_PATH", tmp_path / "running")
    monkeypatch.setattr(boot_sentinel, "REPORT_PATH", tmp_path / "last_boot.json")
    monkeypatch.setattr(boot_sentinel, "pstore_records", list)
    return boot_sentinel


def _reboot(sentinel):
    """Age the stored report so the next on_boot() reads as a new boot, not a restart."""
    report = sentinel.read_report()
    report["boot_id"] = "a-previous-boot"
    sentinel.write_report(report)


def test_a_crash_dump_predating_the_install_is_remembered_but_not_counted(sentinel, monkeypatch):
    """It crashed a boot nobody was measuring; dating it into the window would inflate the rate."""
    monkeypatch.setattr(sentinel, "pstore_records", lambda: [OLD_DUMP])

    sentinel.on_boot()
    report = sentinel.read_report()

    assert report["cause"] == "unknown"
    assert report["ungraceful_count"] == 0
    assert report["pstore_seen"] == [OLD_DUMP[0]], "seen, so the next boot does not re-report it"


def test_a_dump_that_appears_later_is_still_attributed(sentinel, monkeypatch):
    monkeypatch.setattr(sentinel, "pstore_records", lambda: [OLD_DUMP])
    sentinel.on_boot()
    _reboot(sentinel)

    monkeypatch.setattr(sentinel, "pstore_records", lambda: [OLD_DUMP, NEW_DUMP])
    sentinel.on_boot()
    report = sentinel.read_report()

    assert report["cause"] == "kernel_panic"
    assert report["ungraceful_count"] == 1


def test_restarting_the_unit_does_not_invent_a_shutdown(sentinel):
    """ExecStop then ExecStart within one boot would otherwise read as a clean cycle."""
    sentinel.on_boot()
    before = sentinel.read_report()

    sentinel.on_shutdown()
    sentinel.on_boot()

    assert sentinel.read_report()["boot_count"] == before["boot_count"]
    assert sentinel.FLAG_PATH.exists(), "and the flag must be back up"


def test_a_clean_cycle_then_a_yanked_one(sentinel):
    sentinel.on_boot()
    sentinel.on_shutdown()
    _reboot(sentinel)

    sentinel.on_boot()
    clean = sentinel.read_report()
    assert (clean["flag_verdict"], clean["cause"]) == ("graceful", "clean")
    assert clean["ungraceful_count"] == 0

    # No on_shutdown() this time: the power went away and the flag survived.
    _reboot(sentinel)
    sentinel.on_boot()
    yanked = sentinel.read_report()
    assert (yanked["flag_verdict"], yanked["cause"]) == ("ungraceful", "power_loss")
    assert yanked["ungraceful_count"] == 1
    assert yanked["boot_count"] == 3
