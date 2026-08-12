#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Was the last shutdown graceful, and if not, what took the robot down?

Storage selection for MARS turns on how often a robot resets without the SSD
being told first (INN-769).  The drive's own ``unsafe_shutdowns`` counter,
already on the wire via the logger's disk sweep, says *that* a cut was dirty
but never *why* -- and the causes have nothing in common to fix.  A kernel
panic is a software bug, a PCIe link-down is a connector loosening under
vibration, a brownout is power-rail margin.  Counting them together gives one
number nobody can act on.

Three mechanisms run here, because each is blind where the others see:

``flag``
    A file written at boot and removed on clean shutdown.  Present at the next
    boot means the last one never reached ``ExecStop``.  Cheap, and it catches
    panics -- the one class expected to dominate.
``journal``
    The tail of the previous boot's journal.  It cannot see a cut that beat the
    write to disk, but when the record survived it is the only thing that names
    a cause.
``pstore``
    The kernel's own crash dump, which survives a reset precisely because it is
    not a disk write.  A panic usually dies before journald flushes, so this is
    where panics actually show up -- when the platform has ramoops at all.

Both verdicts ship rather than one merged bool: they disagree in ways that are
themselves the signal.  Flag-dirty against a journal with no complaint in it is
the brownout / SYS_RESET signature -- the rail sagged or the carrier asserted
reset, and userspace never learned anything was wrong.

Run as root by innate-boot-sentinel.service: ``boot`` at start, ``shutdown`` at
stop.  Stdlib only and no ROS -- this runs long before the workspace is sourced,
and a robot whose colcon build is broken must still report its own resets.

Exit status is always 0.  A ``oneshot`` unit whose ``ExecStart`` failed is a
failed unit, and systemd does not run ``ExecStop`` for one of those -- so a
crash in the analysis below would silently turn the *next* clean shutdown into
a recorded dirty one.  The measurement must never be able to corrupt itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path("/var/lib/innate/boot")
# Present for exactly as long as the system is up.  Its absence at boot is the
# whole of mechanism 1.
FLAG_PATH = STATE_DIR / "running"
# What the logger node reads and ships as vitals["boot"].  Rewritten once per
# boot; see shutdowns.py on the reading side.
REPORT_PATH = STATE_DIR / "last_boot.json"

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PSTORE_DIR = Path("/sys/fs/pstore")
PERSISTENT_JOURNAL = Path("/var/log/journal")

SCHEMA = 1
# Enough tail to hold a panic trace and the shutdown sequence, small enough
# that the read costs nothing at boot.
TAIL_LINES = 400
EVIDENCE_LINES = 5
EVIDENCE_CHARS = 200
_TIMEOUT = 30.0

# Ordered: the first match wins, so the specific cause beats the general one.
# All of these are matched against the *tail* of the previous boot -- something
# that logged a soft lockup an hour before a clean reboot was survived, and is
# not what ended that boot.
FAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "kernel_panic",
        re.compile(
            r"Kernel panic - not syncing|Internal error: Oops|Unable to handle kernel|BUG: unable to handle|Fatal exception",
            re.IGNORECASE,
        ),
    ),
    (
        "watchdog",
        re.compile(
            r"watchdog: BUG: soft lockup|Watchdog detected hard LOCKUP|watchdog: Watchdog detected|tegra_wdt|hardware watchdog",
            re.IGNORECASE,
        ),
    ),
    (
        # Bare "Link Down" is not usable here -- every ethernet interface logs
        # it on unplug.  Only the PCIe-scoped spellings count.
        "pcie_link_down",
        re.compile(
            r"PCIe link is down|PCIe link down|pcieport[^\n]*[Ll]ink [Dd]own|nvme[^\n]*controller is down|nvme[^\n]*Removing after probe failure",
            re.IGNORECASE,
        ),
    ),
    (
        "brownout",
        re.compile(
            r"soctherm[^\n]*OC ALARM|under-?voltage|VDD_[A-Z0-9_]+[^\n]*fault|input voltage[^\n]*low",
            re.IGNORECASE,
        ),
    ),
)

# systemd-shutdown is the process that runs after everything else is stopped,
# so anything it logged at all means the shutdown path was actually walked.
# The target names carry a distribution-dependent prefix ("Reached target
# System Shutdown", "Reached target Late Shutdown Services"), hence the gap.
#
# Every entry here has to be a *positive* record of shutting down. A false
# clean is the one error that biases the deliverable downwards and cannot be
# spotted afterwards in the data, so nothing merely suggestive belongs.
CLEAN_PATTERN = re.compile(
    r"systemd-shutdown\[|Reached target (?:\w+ )*(?:Power-?Off|Power Off|Reboot|Halt|Shutdown)"
    r"|Shutting down\.|Syncing filesystems and block devices",
    re.IGNORECASE,
)

FAULT_CAUSES = frozenset(name for name, _ in FAULT_PATTERNS)


# ── Verdicts ────────────────────────────────────────────────────────


def classify_journal(tail: str) -> tuple[str, list[str]]:
    """Name the cause the previous boot's journal tail supports, with evidence.

    ``truncated`` is a real answer, not a failure: the log simply stops, which
    is what a surprise cut looks like from the filesystem's side.  Telling it
    apart from a clean stop is the point -- ``clean`` requires a positive
    record, never the absence of a complaint.
    """
    lines = tail.splitlines()
    for cause, pattern in FAULT_PATTERNS:
        matched = [line for line in lines if pattern.search(line)]
        if matched:
            return cause, _evidence(matched)
    if any(CLEAN_PATTERN.search(line) for line in lines):
        return "clean", []
    return "truncated", []


def resolve_cause(flag_verdict: str, journal_verdict: str) -> str:
    """The single label the per-cause breakdown is grouped by.

    The flag is authoritative for *graceful*: ``ExecStop`` demonstrably ran, so
    the shutdown path was walked no matter what else is in the log.  It is the
    journal that is authoritative for *why*, and only once the flag has already
    established that something went wrong.
    """
    if flag_verdict == "graceful":
        return "clean"
    if journal_verdict in FAULT_CAUSES:
        return journal_verdict
    if flag_verdict == "ungraceful":
        # Nothing in the log and no crash dump, but the flag survived: the
        # power went away without the kernel getting a word in.
        return "power_loss"
    return "clean" if journal_verdict == "clean" else "unknown"


def _evidence(lines: list[str]) -> list[str]:
    """A few matched lines, trimmed -- enough to triage without shipping a log."""
    return [line.strip()[:EVIDENCE_CHARS] for line in lines[:EVIDENCE_LINES]]


# ── Sources ─────────────────────────────────────────────────────────


def boot_id() -> str | None:
    try:
        return BOOT_ID_PATH.read_text().strip() or None
    except OSError:
        return None


def journal_tail() -> str | None:
    """The previous boot's last few hundred lines, or None if there is no such boot.

    Distinguishing "no prior boot recorded" from "prior boot recorded nothing"
    matters: with a volatile journal the second never happens and mechanism 2
    is dead on every robot in the fleet, silently.

    short-iso rather than cat: `-o cat` prints the message and nothing else, so
    the syslog identifier goes with it -- and "systemd-shutdown", the strongest
    clean-shutdown marker there is, lives in the identifier. The timestamps are
    worth having in the evidence lines anyway.
    """
    return _run(["journalctl", "-b", "-1", "--no-pager", "-o", "short-iso", "-n", str(TAIL_LINES)])


def journal_boots() -> int:
    """How many boots the journal still holds -- 0 when it is volatile or empty."""
    listing = _run(["journalctl", "--list-boots", "--no-pager", "-q"])
    if not listing:
        return 0
    return len([line for line in listing.splitlines() if line.strip()])


def pstore_records() -> list[tuple[str, str]]:
    """Kernel crash dumps left in pstore, as (identity, first interesting line).

    Read, never removed: systemd-pstore.service archives and clears this
    directory on its own schedule, and racing it would cost the evidence.
    Re-reporting across boots is handled by identity instead -- name plus mtime,
    which a new dump changes and an archived-away one simply stops producing.
    """
    records: list[tuple[str, str]] = []
    try:
        entries = sorted(PSTORE_DIR.iterdir())
    except OSError:
        return records
    for entry in entries:
        if not entry.name.startswith("dmesg-"):
            continue
        try:
            identity = f"{entry.name}:{int(entry.stat().st_mtime)}"
            body = entry.read_text(errors="replace")
        except OSError:
            continue
        records.append((identity, body))
    return records


def _run(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


# ── State ───────────────────────────────────────────────────────────


def read_report() -> dict[str, Any]:
    try:
        loaded = json.loads(REPORT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_report(report: dict[str, Any]) -> None:
    """Replace the report atomically, so a cut mid-write cannot leave a torn one."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_PATH.with_suffix(".tmp")
    _write_durable(temporary, json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(REPORT_PATH)
    # 0644: the logger node reads this as the robot user, not as root.
    os.chmod(REPORT_PATH, 0o644)
    _sync_directory(STATE_DIR)


def write_flag(current_boot: str | None) -> None:
    """Raise the sentinel, and make sure it is on the platter before returning.

    An unflushed flag is worse than no flag: the event being measured is
    precisely a power cut with writes still in cache, and a flag lost to one
    turns a dirty shutdown into a recorded clean one.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_durable(FLAG_PATH, json.dumps({"boot_id": current_boot, "since": int(time.time())}) + "\n")
    _sync_directory(STATE_DIR)


def clear_flag() -> None:
    FLAG_PATH.unlink(missing_ok=True)
    _sync_directory(STATE_DIR)


def _write_durable(path: Path, body: str) -> None:
    with open(path, "w") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    """fsync the directory too -- the file's own fsync does not persist its name."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ── Commands ────────────────────────────────────────────────────────


def on_boot() -> None:
    current_boot = boot_id()
    previous = read_report()

    # `systemctl restart` inside a running session stops and starts the unit,
    # which would clear the flag and then find it missing -- a clean shutdown
    # that never happened, on a robot that never rebooted.  The kernel's boot
    # id is what tells a restart apart from a boot.
    if current_boot is not None and previous.get("boot_id") == current_boot:
        write_flag(current_boot)
        print("boot sentinel: same boot as the last record — restart, not a reboot")
        return

    flag_present = FLAG_PATH.exists()
    first_run = not previous and not flag_present
    if first_run:
        # First run after install.  The last shutdown predates the sentinel, so
        # there is nothing to say about it; saying "clean" would be a guess
        # that biases the number this whole exercise exists to produce.
        flag_verdict = "unknown"
    else:
        flag_verdict = "ungraceful" if flag_present else "graceful"

    tail = journal_tail()
    if tail is None:
        journal_verdict, evidence = "no_prior_boot", []
    else:
        journal_verdict, evidence = classify_journal(tail)

    reported_pstore = set(previous.get("pstore_seen", []))
    fresh_pstore = [(identity, body) for identity, body in pstore_records() if identity not in reported_pstore]
    if first_run and fresh_pstore:
        # A dump already sitting in pstore on the sentinel's first ever run
        # crashed some boot we were not here for, possibly months ago.  Counting
        # it would date an old crash into the measurement window, so record it
        # as seen and say nothing about it -- the same "does not know" the flag
        # gives for this boot.
        reported_pstore |= {identity for identity, _ in fresh_pstore}
        fresh_pstore = []
    if fresh_pstore:
        # The kernel wrote a crash dump, which outranks anything inferred from
        # a log that may have died before the flush.
        journal_verdict, evidence = classify_journal("\n".join(body for _, body in fresh_pstore))
        if journal_verdict not in FAULT_CAUSES:
            journal_verdict = "kernel_panic"

    cause = resolve_cause(flag_verdict, journal_verdict)
    ungraceful = cause not in ("clean", "unknown")

    report = {
        "schema": SCHEMA,
        "recorded_at": int(time.time()),
        "boot_id": current_boot,
        "flag_verdict": flag_verdict,
        "journal_verdict": journal_verdict,
        "cause": cause,
        "evidence": evidence,
        "pstore_records": len(fresh_pstore),
        "boots_in_journal": journal_boots(),
        "journal_persistent": PERSISTENT_JOURNAL.is_dir(),
        # Cumulative, because a per-boot row is only delivered if telemetry
        # happens to be reachable -- and the fleet spends real time offline.
        # These are the same shape as the drive's own counters, and comparable
        # against them.
        "boot_count": int(previous.get("boot_count", 0)) + 1,
        "ungraceful_count": int(previous.get("ungraceful_count", 0)) + (1 if ungraceful else 0),
        "pstore_seen": sorted(reported_pstore | {identity for identity, _ in fresh_pstore}),
    }
    write_report(report)
    write_flag(current_boot)
    print(f"boot sentinel: last shutdown {cause} (flag {flag_verdict}, journal {journal_verdict})")


def on_shutdown() -> None:
    clear_flag()
    print("boot sentinel: flag cleared, this shutdown will read as graceful")


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command not in ("boot", "shutdown"):
        print(f"usage: {Path(argv[0]).name} boot|shutdown", file=sys.stderr)
        return 2

    try:
        on_boot() if command == "boot" else on_shutdown()
    except Exception as error:  # noqa: BLE001 — see the module docstring
        print(f"boot sentinel: {command} failed: {error}", file=sys.stderr)
        if command == "boot":
            # Whatever else went wrong, the next boot still needs a flag to
            # find -- otherwise one bad analysis blinds the mechanism for good.
            try:
                write_flag(boot_id())
            except OSError as flag_error:
                print(f"boot sentinel: could not raise the flag either: {flag_error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
