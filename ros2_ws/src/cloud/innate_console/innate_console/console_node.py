#!/usr/bin/env python3
"""innate_console — tmux-stdout → rosbridge bridge with structured records.

The robot's nodes run in tmux panes (see scripts/launch_ros_in_tmux.sh), and
their *full* stdout — including library/launch lines that never reach /rosout
(e.g. the rws ``asio.system:104`` errors) — only exists in those panes. This
node taps every pane with ``tmux pipe-pane`` (seeding from existing scrollback
first), and exposes that stream over rosbridge.

Because the whole stack now logs in one standard format (set in
config/dds/setup_dds.zsh: ``[{severity}] [{time}] [{name}]: {message}``), and
the launch system prefixes every line with ``[exe-N]``, each line carries its
full identity. We parse it **once, here**, into a structured record so the
webapp just renders the hierarchy (window → process → node → logger) instead of
re-parsing text:

    { t, window, pane, proc, pidx, node, logger, level, msg, text }

``text`` is the raw line (kept for the byte-faithful pane/terminal view and for
backward compatibility with the current webapp, which reads pane/t/text).

Backfill is buffered **per node** (fallback: per process, then per pane) so one
flooding node — e.g. a stuck reconnect loop — can't evict everyone else's
history. Buckets are LRU-capped, which also discards dead per-connection loggers
(rws ``client_handler_N``) without starving real nodes.

Deliberately tiny: rclpy + std_msgs + stdlib only, no custom interfaces.
"""

import json
import os
import re
import subprocess
import time
from collections import OrderedDict, deque

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

def _current_tmux_session() -> str:
    """Tap whichever tmux session launched us — 'ros_nodes' on the robot, 'innate'
    in the sim — so the session name never has to be configured. Falls back to the
    robot default when not running under tmux."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        name = result.stdout.strip()
        if result.returncode == 0 and name:
            return name
    except Exception:
        pass
    return "ros_nodes"


TMUX_SESSION = _current_tmux_session()
CAPTURE_DIR = os.path.expanduser("~/.ros/innate_console")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

PER_NODE_LINES = 120  # backfill records kept per bucket (node/proc/pane)
MAX_BUCKETS = 200  # cap distinct buckets; LRU-evict the oldest. Bounds
# memory and discards dead per-connection loggers
# (client_handler_N) without starving real nodes.
POLL_HZ = 5.0  # how often to drain the pane capture files
REWIRE_SEC = 10.0  # how often to pick up newly-appeared panes
MAX_LINES_PER_DRAIN = 400  # per-pane flood cap per poll tick
FILE_CAP_BYTES = 4_000_000  # truncate a capture file once it grows past this
SCROLLBACK_LINES = 200  # tmux history lines to seed per pane on first wire

# Standard launched rcl line: "[exe-N] [LEVEL] [ts] [name]: msg".
STD_RE = re.compile(
    r"^\[(?P<proc>[^\]]+?)-(?P<idx>\d+)\]\s+"
    r"\[(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\]\s+"
    r"\[(?P<ts>\d+\.\d+)\]\s+"
    r"\[(?P<name>[^\]]+)\]:\s?(?P<msg>.*)$"
)
# Bare rcl line — no launch "[exe-N]" prefix (a `ros2 run` process, e.g.
# training_node): "[LEVEL] [ts] [name]: msg". The process *is* the node.
BARE_RE = re.compile(
    r"^\[(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\]\s+"
    r"\[(?P<ts>\d+\.\d+)\]\s+"
    r"\[(?P<name>[^\]]+)\]:\s?(?P<msg>.*)$"
)
# "[LEVEL] [who]: msg" — launch-system output. `who` is "exe-N" (process start
# messages) or a bare name like "launch" (ros2 launch's own banner).
SYS_RE = re.compile(r"^\[(?P<level>DEBUG|INFO|WARN|ERROR|FATAL)\]\s+\[(?P<who>[^\]]+)\]:\s?(?P<msg>.*)$")
# Fallback: any leading "[exe-N]" prefix (zenoh/middleware/library lines).
PROC_RE = re.compile(r"^\[(?P<proc>[^\]]+?)-(?P<idx>\d+)\]\s?(?P<msg>.*)$")
WHO_RE = re.compile(r"^(?P<proc>.*)-(?P<idx>\d+)$")  # split "exe-N"


def _basename(name):
    return name.lstrip("/").split("/")[-1] or name


def _parse(text):
    """Parse a console line → (proc, pidx, node, logger, level, ts|None, msg).

    proc/node are None only when the line carries no identity at all (then the
    caller carries forward the previous line's identity, e.g. for traceback
    continuation lines)."""
    m = STD_RE.match(text)
    if m:
        logger = m["name"]
        node = _basename(logger).split(".")[0]  # child loggers: node = "node.sub"
        return (m["proc"], int(m["idx"]), node, logger, m["level"], float(m["ts"]), m["msg"])
    m = BARE_RE.match(text)
    if m:
        logger = m["name"]
        node = _basename(logger).split(".")[0]
        return (node, None, node, logger, m["level"], float(m["ts"]), m["msg"])  # proc == node
    m = SYS_RE.match(text)
    if m:
        wm = WHO_RE.match(m["who"])
        if wm:
            return (wm["proc"], int(wm["idx"]), None, None, m["level"], None, m["msg"])
        return (m["who"], None, None, None, m["level"], None, m["msg"])  # e.g. "launch"
    m = PROC_RE.match(text)
    if m:
        return (m["proc"], int(m["idx"]), None, None, None, None, m["msg"])
    return (None, None, None, None, None, None, text)


class ConsoleBridge(Node):
    def __init__(self):
        super().__init__("innate_console")
        os.makedirs(CAPTURE_DIR, exist_ok=True)

        # bucket key -> deque of recent records; OrderedDict for LRU eviction.
        self._buckets = OrderedDict()
        self._live = self.create_publisher(String, "/innate/console", 50)
        self._backfill = self.create_publisher(String, "/innate/console/backfill", 1)
        self.create_subscription(String, "/innate/console/request", self._on_request, 1)

        # pane_id -> {"window", "pane", "path", "fh", "carry"}
        self._panes = {}
        self._wire_panes()
        self.create_timer(1.0 / POLL_HZ, self._drain_panes)
        self.create_timer(REWIRE_SEC, self._wire_panes)
        self.get_logger().info(f"innate_console up; tapping tmux '{TMUX_SESSION}' (structured, per-node fair buffer)")

    # ---- tmux capture ----------------------------------------------------
    def _wire_panes(self):
        """Tap any pane we're not already capturing (idempotent; handles late
        panes after a relaunch). Existing taps are left untouched."""
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-s", "-t", TMUX_SESSION, "-F", "#{pane_id}\t#{window_name}\t#{pane_index}"],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
        except Exception:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3 or parts[0] in self._panes:
                continue
            pane_id, window, idx = parts
            pane = f"{window}.{idx}"
            # Seed from existing scrollback first — pipe-pane only captures NEW
            # output, so panes that logged at startup then went quiet would
            # otherwise appear empty.
            self._seed_scrollback(pane_id, window, pane)
            path = os.path.join(CAPTURE_DIR, f"{pane_id.lstrip('%')}.log")
            try:
                open(path, "w").close()  # fresh file, then tap the pane into it
                subprocess.run(["tmux", "pipe-pane", "-t", pane_id, f"cat >> '{path}'"], timeout=3, check=False)
                fh = open(path, errors="replace")
            except Exception:
                continue
            self._panes[pane_id] = {"window": window, "pane": pane, "path": path, "fh": fh, "carry": "", "ctx": {}}

    def _seed_scrollback(self, pane_id, window, pane):
        """Buffer (don't publish) a pane's existing tmux scrollback as history."""
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-S", f"-{SCROLLBACK_LINES}", "-t", pane_id],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
        except Exception:
            return
        last_t = 0.0
        ctx = {}
        started = False
        for line in out.splitlines():
            text = ANSI_RE.sub("", line).rstrip("\r")
            if not text:
                continue
            # Drop the pre-launch shell prompt + command echo: everything before
            # the first bracketed log line is terminal cruft, not node output.
            if not started:
                if not text.startswith("["):
                    continue
                started = True
            rec = self._make(window, pane, text, last_t, ctx)
            last_t = rec["t"]  # carry timestamp forward to lines that lack one
            self._store(rec)

    def _drain_panes(self):
        for st in self._panes.values():
            try:
                chunk = st["fh"].read()
            except Exception:
                continue
            if chunk:
                data = st["carry"] + chunk
                lines = data.split("\n")
                st["carry"] = lines.pop()  # keep the partial trailing line
                if len(lines) > MAX_LINES_PER_DRAIN:
                    dropped = len(lines) - MAX_LINES_PER_DRAIN
                    lines = lines[-MAX_LINES_PER_DRAIN:]
                    self._emit(
                        self._make(
                            st["window"], st["pane"], f"… {dropped} lines suppressed (flood) …", time.time(), st["ctx"]
                        )
                    )
                for raw in lines:
                    text = ANSI_RE.sub("", raw).rstrip("\r")
                    if text:
                        self._emit(self._make(st["window"], st["pane"], text, time.time(), st["ctx"]))
            # Keep the capture file from growing forever (the ring buffer holds history).
            try:
                if st["fh"].tell() > FILE_CAP_BYTES and not st["carry"]:
                    open(st["path"], "w").close()  # truncates the shared inode
                    st["fh"].seek(0)
            except Exception:
                pass

    # ---- records + buffering --------------------------------------------
    def _make(self, window, pane, text, fallback_t, ctx):
        """Build a record. `ctx` is per-pane parse state: a line with no identity
        of its own (a multi-line/traceback continuation) inherits the previous
        line's process/node so it groups with its parent instead of "(other)"."""
        proc, pidx, node, logger, level, ts, msg = _parse(text)
        if proc is not None:
            ctx["proc"], ctx["pidx"], ctx["node"] = proc, pidx, node
        elif ctx.get("proc") is not None:
            proc, pidx, node = ctx["proc"], ctx["pidx"], ctx["node"]
        return {
            "t": ts if ts is not None else fallback_t,
            "window": window,
            "pane": pane,
            "proc": proc,
            "pidx": pidx,
            "node": node,
            "logger": logger,
            "level": level,
            "msg": msg,
            "text": text,
        }

    @staticmethod
    def _bucket_key(rec):
        if rec["node"]:
            return "n:" + rec["node"]
        if rec["proc"] is not None:
            return f"p:{rec['proc']}-{rec['pidx']}"
        return "x:" + rec["pane"]

    def _store(self, rec):
        key = self._bucket_key(rec)
        dq = self._buckets.get(key)
        if dq is None:
            dq = deque(maxlen=PER_NODE_LINES)
            self._buckets[key] = dq
            if len(self._buckets) > MAX_BUCKETS:
                self._buckets.popitem(last=False)  # evict least-recently-active
        else:
            self._buckets.move_to_end(key)
        dq.append(rec)

    def _emit(self, rec):
        self._store(rec)
        m = String()
        m.data = json.dumps(rec, separators=(",", ":"))
        self._live.publish(m)

    def _on_request(self, msg: String):
        """Reply on /innate/console/backfill with a fair tail across buckets.
        Request JSON (all optional): {max_lines, node, pane}."""
        try:
            req = json.loads(msg.data) if msg.data.strip() else {}
        except Exception:
            req = {}
        n = max(1, int(req.get("max_lines", 1000)))
        node, pane = req.get("node"), req.get("pane")

        buckets = list(self._buckets.values())
        # Fair share: each bucket contributes its most-recent `quota` lines, so a
        # flooding node can't crowd out quiet ones in the backfill.
        quota = max(1, n // len(buckets)) if buckets else n
        items = []
        for dq in buckets:
            items.extend(list(dq)[-quota:])
        if node is not None:
            items = [r for r in items if r["node"] == node]
        if pane is not None:
            items = [r for r in items if r["pane"] == pane]
        items.sort(key=lambda r: r["t"])
        out = String()
        out.data = json.dumps({"entries": items[-n:]}, separators=(",", ":"))
        self._backfill.publish(out)

    def destroy_node(self):
        # Release the per-pane capture file handles so fds don't leak if the node
        # is re-instantiated in the same process (e.g. a composable-node restart).
        for st in self._panes.values():
            try:
                st["fh"].close()
            except Exception:
                pass
        self._panes.clear()
        super().destroy_node()


def main():
    rclpy.init()
    node = ConsoleBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
