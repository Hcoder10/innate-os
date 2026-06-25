#!/usr/bin/env python3
"""Diagnostic consumer / tester for the mars_cam ``webrtc_streamer`` node.

This acts as the WebRTC *answerer* (the node is the offerer), signalling over the
same ROS2 ``/webrtc/*`` ``std_msgs/String`` topics the web client uses, and
decoding the VP8 / Opus tracks with GStreamer's ``webrtcbin`` to prove that media
actually flows. It mirrors the node's own media stack (VP8/Opus, libnice ICE) so
no extra packages are needed.

It doubles as a probe for the three capabilities we're about to design, so the
"before" baseline is measured against a live node:

  1. lazy subscriptions   — does the node subscribe to cameras only while a peer
                            is connected?
  2. dynamic topic        — can a client ask for an arbitrary topic in START?
  3. multiple streams      — can two peers stream concurrently, and is a
                            "concurrent encoders" count published on a ROS topic?

Usage (ROS sourced, node + cameras live):

    python3 webrtc_consumer.py            # full suite (baseline + the 3 probes)
    python3 webrtc_consumer.py connect    # connect, stream, print per-track FPS
    python3 webrtc_consumer.py lazy
    python3 webrtc_consumer.py dynamic
    python3 webrtc_consumer.py multi
    python3 webrtc_consumer.py connect --audio --duration 10
"""

import argparse
import json
import sys
import threading
import time
import uuid

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402

import rclpy  # noqa: E402
from rclpy.executors import MultiThreadedExecutor  # noqa: E402
from std_msgs.msg import String  # noqa: E402

START_TOPIC = "/webrtc/start"
OFFER_TOPIC = "/webrtc/offer"
ANSWER_TOPIC = "/webrtc/answer"
ICE_IN_TOPIC = "/webrtc/ice_in"
ICE_OUT_TOPIC = "/webrtc/ice_out"

NODE_NAME = "webrtc_streamer"
# Hardware launch subscribes to these raw image topics (use_compressed_images=False).
MAIN_TOPIC = "/mars/main_camera/left/image_raw"
ARM_TOPIC = "/mars/arm/image_raw"
STUN = "stun://stun.l.google.com:19302"

# rtp payload types the node assigns: pt96 = main cam, pt97 = arm cam, pt98 = opus mic.
_PT_LABELS = {96: "main(pt96)", 97: "arm(pt97)", 98: "audio(pt98)"}


def _conn_state_name(webrtc):
    try:
        return webrtc.get_property("connection-state").value_nick
    except Exception:
        return "unknown"


class TrackStat:
    """Per-track decoded-frame counter, fed from an appsink ``new-sample``."""

    def __init__(self, label):
        self.label = label
        self.count = 0
        self.first_t = None
        self.last_t = None

    def tick(self):
        now = time.monotonic()
        if self.first_t is None:
            self.first_t = now
        self.last_t = now
        self.count += 1

    def fps(self):
        if self.count < 2 or self.first_t is None or self.last_t <= self.first_t:
            return 0.0
        return (self.count - 1) / (self.last_t - self.first_t)


class WebRTCPeer:
    """One answering peer: a dedicated rclpy node + a webrtcbin that decodes tracks.

    All peers share the global ``/webrtc/*`` topics (the node has no per-client
    routing), so offers are only accepted between this peer's own ``start()`` and
    the moment its remote description is set.
    """

    def __init__(self, name, source="live", audio=False, start_extra=None):
        self.name = name
        self.source = source
        self.audio = audio
        self.start_extra = start_extra or {}
        self.client_id = str(uuid.uuid4())

        self.started = False
        self.remote_set = False
        self._ice_queue = []  # remote candidates that arrived before remote desc
        self._ice_lock = threading.Lock()
        self.stats = {}  # label -> TrackStat
        self._stats_lock = threading.Lock()
        self.connected_at = None

        self.node = rclpy.create_node(name)
        self.start_pub = self.node.create_publisher(String, START_TOPIC, 10)
        self.answer_pub = self.node.create_publisher(String, ANSWER_TOPIC, 10)
        self.ice_in_pub = self.node.create_publisher(String, ICE_IN_TOPIC, 10)
        self.node.create_subscription(String, OFFER_TOPIC, self._on_offer, 10)
        self.node.create_subscription(String, ICE_OUT_TOPIC, self._on_ice_out, 10)

        self._build_pipeline()

    def _build_pipeline(self):
        self.pipe = Gst.Pipeline.new(f"{self.name}-pipe")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", "recv")
        self.webrtc.set_property("bundle-policy", "max-bundle")
        self.webrtc.set_property("stun-server", STUN)
        self.pipe.add(self.webrtc)
        self.webrtc.connect("on-ice-candidate", self._on_local_ice)
        self.webrtc.connect("pad-added", self._on_pad_added)
        # Diagnostic: count RTCP this consumer sends/receives (proves it behaves like a real
        # receiver). webrtcbin's internal rtpbin exposes send_rtcp_src / recv_rtcp_sink pads.
        self.rtcp_sent = 0
        self.rtcp_recv = 0
        self._rtcp_tuned = False
        self.rtpbin = self.webrtc.get_by_name("rtpbin")
        if self.rtpbin:
            self.rtpbin.connect("pad-added", self._on_rtpbin_pad)
        self.pipe.set_state(Gst.State.PLAYING)

    def _on_rtpbin_pad(self, _rtpbin, pad):
        name = pad.get_name()
        if name.startswith("send_rtcp_src"):
            pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self._count_rtcp_sent)
        elif name.startswith("recv_rtcp_sink"):
            pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self._count_rtcp_recv)
        # Tune session 0 to emit RTCP at a browser-like sub-second cadence (a real Chrome/aiortc
        # receiver sends RR ~1/s + transport-cc ~10-20/s). webrtcbin's default RR interval is several
        # seconds, which is unrepresentative and would graze the node's inactivity threshold.
        if not self._rtcp_tuned:
            try:
                sess = self.rtpbin.emit("get-internal-session", 0)
                if sess:
                    sess.set_property("rtcp-min-interval", 250_000_000)  # 250 ms
                    sess.set_property("rtcp-rr-bandwidth", 100_000)       # generous => frequent RR
                    self._rtcp_tuned = True
            except Exception:
                pass

    def _count_rtcp_sent(self, *_):
        self.rtcp_sent += 1
        return Gst.PadProbeReturn.OK

    def _count_rtcp_recv(self, *_):
        self.rtcp_recv += 1
        return Gst.PadProbeReturn.OK

    # ---- public ----------------------------------------------------------

    def start(self):
        """(Re)send START and accept the next offer. Mirrors the web client's rebuild."""
        self.started = True
        self.remote_set = False
        with self._ice_lock:
            self._ice_queue = []
        with self._stats_lock:
            self.stats = {}
        self.connected_at = None
        payload = {"source": self.source, "audio": self.audio, "client_id": self.client_id}
        payload.update(self.start_extra)
        self.start_pub.publish(String(data=json.dumps(payload)))

    def rebuild(self):
        """Tear down and recreate the webrtcbin — recovers from the bundled-multistream
        pad race in GStreamer 1.20's webrtcbin (same self-heal the web client does)."""
        try:
            self.pipe.set_state(Gst.State.NULL)
        except Exception:
            pass
        self._build_pipeline()

    def conn_state(self):
        return _conn_state_name(self.webrtc)

    def labels(self):
        with self._stats_lock:
            return dict(self.stats)

    def total_frames(self):
        with self._stats_lock:
            return sum(s.count for s in self.stats.values())

    def video_tracks(self):
        with self._stats_lock:
            return [k for k in self.stats if "audio" not in k]

    def destroy(self):
        try:
            self.pipe.set_state(Gst.State.NULL)
        except Exception:
            pass
        self.node.destroy_node()

    # ---- signalling ------------------------------------------------------

    def _on_offer(self, msg):
        # Only the offer that follows our own START, and only once.
        if not self.started or self.remote_set:
            return
        sdp_text = msg.data
        if not sdp_text:
            return

        _, sdpmsg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(sdp_text.encode(), sdpmsg)
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)

        p = Gst.Promise.new()
        self.webrtc.emit("set-remote-description", offer, p)
        p.wait()
        self.remote_set = True

        with self._ice_lock:
            for mline, cand in self._ice_queue:
                self.webrtc.emit("add-ice-candidate", mline, cand)
            self._ice_queue = []

        ap = Gst.Promise.new()
        self.webrtc.emit("create-answer", None, ap)
        ap.wait()
        reply = ap.get_reply()
        answer = reply.get_value("answer")

        lp = Gst.Promise.new()
        self.webrtc.emit("set-local-description", answer, lp)
        lp.wait()
        self.answer_pub.publish(String(data=answer.sdp.as_text()))

    def _on_local_ice(self, _webrtc, mline, candidate):
        self.ice_in_pub.publish(
            String(data=json.dumps({"candidate": candidate, "sdpMLineIndex": mline}))
        )

    def _on_ice_out(self, msg):
        try:
            parsed = json.loads(msg.data)
        except (ValueError, TypeError):
            return
        cand = parsed.get("candidate")
        if not cand:
            return
        mline = int(parsed.get("sdpMLineIndex", 0))
        with self._ice_lock:
            if not self.remote_set:
                self._ice_queue.append((mline, cand))
                return
        self.webrtc.emit("add-ice-candidate", mline, cand)

    # ---- media -----------------------------------------------------------

    def _on_pad_added(self, _webrtc, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps() or pad.query_caps(None)
        s = caps.get_structure(0)
        pt = s.get_int("payload")[1] if s.has_field("payload") else None
        enc = s.get_string("encoding-name")
        label = _PT_LABELS.get(pt, enc or f"pt{pt}")
        with self._stats_lock:
            if label in self.stats:  # duplicate pad from a rebuild/race — ignore
                return
        if self.connected_at is None:
            self.connected_at = time.monotonic()

        if enc == "OPUS":
            chain = ["rtpopusdepay", "opusdec"]
        else:  # VP8
            chain = ["rtpvp8depay", "vp8dec", "videoconvert"]

        elems = [Gst.ElementFactory.make(name) for name in chain]
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("drop", True)
        sink.set_property("max-buffers", 1)
        stat = TrackStat(label)
        with self._stats_lock:
            self.stats[label] = stat
        sink.connect("new-sample", self._on_sample, stat)

        elems.append(sink)
        for e in elems:
            self.pipe.add(e)
            e.sync_state_with_parent()
        Gst.Element.link_many(*elems)
        pad.link(elems[0].get_static_pad("sink"))

    def _on_sample(self, sink, stat):
        sink.emit("pull-sample")  # frees the buffer
        stat.tick()
        return Gst.FlowReturn.OK


class Harness:
    """Owns the GLib main loop and a multi-threaded rclpy executor."""

    def __init__(self):
        rclpy.init()
        Gst.init(None)
        self.loop = GLib.MainLoop()
        self._glib_t = threading.Thread(target=self.loop.run, daemon=True)
        self._glib_t.start()
        self.executor = MultiThreadedExecutor()
        self._stop = threading.Event()
        self._spin_t = threading.Thread(target=self._spin, daemon=True)
        self._spin_t.start()
        self.probe = rclpy.create_node("webrtc_tester_probe")
        self.executor.add_node(self.probe)

    def _spin(self):
        while not self._stop.is_set():
            self.executor.spin_once(timeout_sec=0.1)

    def add(self, peer):
        self.executor.add_node(peer.node)

    def remove(self, peer):
        self.executor.remove_node(peer.node)

    def node_subscriptions(self, node_name=NODE_NAME):
        """Topics ``node_name`` currently subscribes to (graph view)."""
        try:
            info = self.probe.get_subscriber_names_and_types_by_node(node_name, "/")
        except Exception:
            return set()
        return {name for name, _types in info}

    def node_is_up(self):
        return self.probe.count_publishers(OFFER_TOPIC) > 0 or NODE_NAME in [
            n for n, _ns in self.probe.get_node_names_and_namespaces()
        ]

    def topic_names(self):
        return {name for name, _types in self.probe.get_topic_names_and_types()}

    def shutdown(self):
        self._stop.set()
        self._spin_t.join(timeout=2)
        self.loop.quit()
        self.probe.destroy_node()
        rclpy.shutdown()


# ---- helpers -------------------------------------------------------------


def wait_for(predicate, timeout, poll=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def connect_peer(h, peer, timeout=12.0, want_video=1, attempts=3):
    """Add, start, and wait until the peer decodes ``want_video`` tracks.

    Retries by rebuilding the webrtcbin (the GStreamer 1.20 bundled-multistream pad
    race intermittently drops a track), mirroring the web client's watchdog rebuild.
    Returns True on success.
    """
    if peer.node not in h.executor.get_nodes():
        h.add(peer)
    for attempt in range(attempts):
        if attempt > 0:
            peer.rebuild()
        peer.start()

        def ready():
            return (peer.conn_state() == "connected"
                    and peer.total_frames() > 0
                    and len(peer.video_tracks()) >= want_video)

        if wait_for(ready, timeout):
            return True
    return False


def fmt_labels(peer):
    rows = []
    for label, st in sorted(peer.labels().items()):
        rows.append(f"      {label:14} {st.count:5d} frames  {st.fps():5.1f} fps")
    return "\n".join(rows) if rows else "      (no tracks)"


# ---- tests ---------------------------------------------------------------


def test_connect(h, source="live", audio=False, duration=8.0):
    print(f"\n[baseline] connect (source={source}, audio={audio}, window={duration}s)")
    peer = WebRTCPeer("webrtc_tester_c0", source=source, audio=audio)
    try:
        if not connect_peer(h, peer, want_video=2):
            print(f"  FAIL  no media; connection-state={peer.conn_state()} "
                  f"tracks={peer.video_tracks()}")
            return False
        print(f"  connected ({peer.conn_state()}); measuring {duration}s ...")
        # reset counters for a clean fps window
        with peer._stats_lock:
            for st in peer.stats.values():
                st.count, st.first_t, st.last_t = 0, None, None
        time.sleep(duration)
        print(fmt_labels(peer))
        labels = peer.labels()
        have_main = any("main" in k for k in labels)
        have_arm = any("arm" in k for k in labels)
        have_audio = any("audio" in k for k in labels)
        print(f"  main video: {'OK' if have_main else 'MISSING'}   "
              f"arm video: {'OK' if have_arm else 'MISSING'}   "
              f"audio: {'OK' if have_audio else ('off' if not audio else 'MISSING (mic fell back?)')}")
        ok = have_main and have_arm and (have_audio or not audio)
        print(f"  {'PASS' if ok else 'FAIL'}")
        return ok
    finally:
        peer.destroy()
        h.remove(peer)


def _camera_subs(h):
    """Camera topics the node currently subscribes to (subset of {main, arm}).

    NB: the in-process ROS graph query is eventually-consistent (and flaky under Zenoh during
    connection churn), so callers must debounce — never trust a single read mid-connection.
    """
    subs = h.node_subscriptions()
    return sorted(t for t in (MAIN_TOPIC, ARM_TOPIC) if t in subs)


def _subs_empty_stable(h, checks=4, interval=0.5):
    """True only if the node's camera subs read empty across several consecutive polls — filters
    the spurious single-read empties the Zenoh graph returns during connection churn."""
    for _ in range(checks):
        if _camera_subs(h):
            return False
        time.sleep(interval)
    return True


def test_lazy(h, rtcp_timeout=5.0):
    print("\n[goal 1: lazy subs] cameras subscribed only while a peer is connected (RTCP watchdog)")

    # 1) cold idle: the node holds NO camera subs until a peer connects (empty is the steady state).
    cold = wait_for(lambda: _subs_empty_stable(h), rtcp_timeout + 20)
    print(f"  idle (no consumer):     camera_subs={_camera_subs(h)}  -> {'none (good)' if cold else 'STILL PRESENT'}")

    peer = WebRTCPeer("webrtc_tester_lazy", source="live")
    flowing = no_false_teardown = released = False
    latency = float("nan")
    try:
        connect_peer(h, peer, want_video=2, timeout=20)
        time.sleep(2.0)  # let the connect-time rebuild flicker settle
        print(f"  during connection:      camera_subs={_camera_subs(h)} (graph; frames are the real signal)")

        # 2/3) Hold past the threshold. The node-side truth is FRAME FLOW: if the watchdog wrongly
        #      tore down a live peer, encoding stops and frames stop. Sample the LAST 3 s of the hold
        #      so a mid-hold teardown (e.g. at ~threshold) is caught, not masked by early frames.
        hold = rtcp_timeout * 2 + 4
        print(f"  holding live connection {hold:.0f}s (> {rtcp_timeout}s threshold) — frames must keep flowing ...")
        f0 = peer.total_frames()
        time.sleep(hold - 3)
        fmid = peer.total_frames()
        time.sleep(3)
        f1 = peer.total_frames()
        total = f1 - f0
        last3 = f1 - fmid
        flowing = total > 0
        no_false_teardown = last3 > 30  # >~10 fps still arriving at the END => never torn down
        print(f"  while held:             frames+={total} (last 3s: {last3})  "
              f"-> {'still flowing at end (good)' if no_false_teardown else 'STALLED — live conn torn down (bad)'}")
    finally:
        peer.destroy()
        h.remove(peer)

    # 4) prompt release after the peer vanishes: RTCP stops, watchdog fires within ~threshold, and
    #    the subs go (and STAY) empty.
    t0 = time.monotonic()
    released = wait_for(lambda: _subs_empty_stable(h), rtcp_timeout + 25, poll=0.5)
    latency = time.monotonic() - t0
    print(f"  after peer killed:      released={released} in ~{latency:.1f}s (threshold {rtcp_timeout}s)")

    lazy = cold and flowing and no_false_teardown and released
    print(f"  {'PASS (lazy)' if lazy else 'GAP — lazy/teardown not working as expected'}")
    return lazy


def test_dynamic(h):
    print("\n[goal 2: dynamic topic] can a client request an arbitrary topic in START?")
    custom = "/mars/main_camera/left/image_rect_color"
    before = h.node_subscriptions()
    print(f"  proposing START field {{\"topic\": \"{custom}\"}}")
    peer = WebRTCPeer("webrtc_tester_dyn", source="live", start_extra={"topic": custom})
    try:
        connect_peer(h, peer, timeout=20)
        time.sleep(1.5)
        after = h.node_subscriptions()
        subscribed = custom in after and custom not in before
        print(f"  node subscribed to requested topic: {subscribed}")
        msg = "PASS" if subscribed else "GAP — START ignores unknown fields; source fixed to live/replay params"
        print(f"  {msg}")
        return subscribed
    finally:
        peer.destroy()
        h.remove(peer)


def test_multi(h):
    print("\n[goal 3: multiple streams] concurrent peers + a published concurrent-encoder count?")
    # 3a: is there a ROS topic publishing the live encoder/stream count?
    candidates = [
        "/webrtc/active_streams", "/webrtc/stream_count", "/webrtc/encoders",
        "/webrtc/active_encoders", "/webrtc/streams",
    ]
    topics = h.topic_names()
    found = [t for t in candidates if t in topics]
    print(f"  concurrent-count topic: {found if found else 'NONE found'}")

    # 3b: can two peers stream at once, or does the 2nd preempt the 1st?
    a = WebRTCPeer("webrtc_tester_a", source="live")
    b = WebRTCPeer("webrtc_tester_b", source="live")
    try:
        if not connect_peer(h, a, timeout=20):
            print(f"  FAIL  peer A never connected (state={a.conn_state()})")
            return False
        base = a.total_frames()
        time.sleep(2.0)
        a_rate_before = a.total_frames() - base
        print(f"  peer A streaming (state={a.conn_state()}, {a_rate_before} frames/2s)")

        print("  peer B connecting ...")
        connect_peer(h, b, timeout=20)
        time.sleep(3.0)

        a_mid = a.total_frames()
        time.sleep(2.0)
        a_rate_after = a.total_frames() - a_mid
        b_frames = b.total_frames()
        print(f"  after B joined:  A state={a.conn_state()} ({a_rate_after} frames/2s)   "
              f"B state={b.conn_state()} ({b_frames} frames)")

        concurrent = a_rate_after > 0 and b_frames > 0
        if concurrent:
            print("  PASS — both peers streaming concurrently")
        else:
            print("  GAP — single pipeline: 2nd START rebuilds the node, preempting peer A "
                  "(matches the web client's 'last-active-wins'); no shared-encode fan-out")
        return concurrent and bool(found)
    finally:
        a.destroy(); h.remove(a)
        b.destroy(); h.remove(b)


def run_full(h, args):
    results = {}
    results["baseline"] = test_connect(h, source=args.source, audio=args.audio, duration=args.duration)
    results["goal1_lazy"] = test_lazy(h, rtcp_timeout=args.rtcp_timeout)
    results["goal2_dynamic"] = test_dynamic(h)
    results["goal3_multi"] = test_multi(h)

    print("\n==================== SUMMARY ====================")
    for k, v in results.items():
        tag = "PASS" if v else "GAP/FAIL"
        print(f"  {k:16} {tag}")
    print("================================================")
    print("Goals 1-3 reporting GAP is expected against today's node — that's the baseline "
          "the build will close.")
    return all(results.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="full",
                    choices=["full", "connect", "lazy", "dynamic", "multi"])
    ap.add_argument("--source", default="live", choices=["live", "replay"])
    ap.add_argument("--audio", action="store_true", help="request the robot mic track")
    ap.add_argument("--duration", type=float, default=8.0, help="fps measurement window (s)")
    ap.add_argument("--rtcp-timeout", type=float, default=5.0,
                    help="node's rtcp_inactivity_timeout_s, used to size the lazy test's waits")
    args = ap.parse_args()

    h = Harness()
    try:
        if not wait_for(h.node_is_up, 5.0):
            print("ERROR: webrtc_streamer node not found on the ROS graph. "
                  "Launch it first:\n  ros2 launch mars_cam webrtc_streamer.launch.py")
            return 2
        if args.mode == "full":
            ok = run_full(h, args)
        elif args.mode == "connect":
            ok = test_connect(h, source=args.source, audio=args.audio, duration=args.duration)
        elif args.mode == "lazy":
            ok = test_lazy(h, rtcp_timeout=args.rtcp_timeout)
        elif args.mode == "dynamic":
            ok = test_dynamic(h)
        elif args.mode == "multi":
            ok = test_multi(h)
        return 0 if ok else 1
    finally:
        h.shutdown()


if __name__ == "__main__":
    sys.exit(main())
