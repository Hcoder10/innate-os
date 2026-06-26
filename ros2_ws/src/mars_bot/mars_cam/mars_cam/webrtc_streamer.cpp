#include "mars_cam/webrtc_streamer.hpp"

#include <gst/rtp/rtp.h>
#include <gst/app/app.h>

#include <arpa/inet.h>
#include <netdb.h>

#include <sstream>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <chrono>
#include <future>
#include <memory>
#include <thread>

namespace mars_cam {

namespace {
GstWebRTCPeerConnectionState peer_connection_state(GstElement* webrtc) {
    GstWebRTCPeerConnectionState state;
    g_object_get(webrtc, "connection-state", &state, nullptr);
    return state;
}

const char* conn_state_name(GstWebRTCPeerConnectionState s) {
    switch (s) {
        case GST_WEBRTC_PEER_CONNECTION_STATE_NEW: return "new";
        case GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTING: return "connecting";
        case GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED: return "connected";
        case GST_WEBRTC_PEER_CONNECTION_STATE_DISCONNECTED: return "disconnected";
        case GST_WEBRTC_PEER_CONNECTION_STATE_FAILED: return "failed";
        case GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED: return "closed";
    }
    return "unknown";
}
double round1(double v) { return std::round(v * 10.0) / 10.0; }

bool wants(const std::vector<std::string>& videos, const std::string& cam) {
    return std::find(videos.begin(), videos.end(), cam) != videos.end();
}

// Each camera gets a fixed SSRC (base + 1-based index), declared in every peer's transport caps so the
// SDP offer carries a=ssrc/msid (built before any RTP has flowed, so webrtcbin can't infer it). All peers
// share a camera's SSRC — fine, since each peer is an independent SRTP transport.
guint cam_ssrc_for_index(size_t index) { return 0x1A2B3C00u + static_cast<guint>(index) + 1u; }

// RTP payload type per camera (96, 97, 99, 100, …) — skips 98, which the audio (opus) payloader uses.
int cam_pt_for_index(size_t index) {
    const int pt = 96 + static_cast<int>(index);
    return pt >= 98 ? pt + 1 : pt;
}

// Chrome/Firefox obfuscate their host ICE candidates as "<uuid>.local" mDNS names (one per local
// interface). On this robot only the reachable (LAN) one resolves quickly (~30 ms); the others never
// resolve and time out after ~5 s. Handing the raw names to libnice makes it block on that 5 s mDNS
// timeout before it finishes nominating, which is what delayed the first video frame. So we resolve each
// .local ourselves with a short deadline, forward only the ones that resolve (rewritten to their IP so
// libnice never resolves again), and drop the slow/unresolvable ones. The fast LAN candidate is enough to
// connect. None of this touches the candidates we *send* (IPv4/tailscale/IPv6 are all still advertised).
std::string candidate_address(const std::string& cand) {
    std::istringstream iss(cand);
    std::string tok;
    for (int idx = 0; iss >> tok; ++idx) {
        if (idx == 4) return tok;  // candidate:<foundation> <comp> <proto> <prio> <ADDRESS> <port> typ ...
    }
    return "";
}

bool is_mdns_address(const std::string& addr) {
    return addr.size() >= 6 && addr.compare(addr.size() - 6, 6, ".local") == 0;
}

// Resolve a name to an IPv4 string, giving up after timeout_ms. getaddrinfo can't be cancelled, so it
// runs on a detached thread that only touches the captured promise (never node state) — safe even if it
// outlives this call: it just finishes the lookup and sets a value nobody reads.
std::string resolve_ipv4_timeout(const std::string& host, int timeout_ms) {
    auto promise = std::make_shared<std::promise<std::string>>();
    std::future<std::string> fut = promise->get_future();
    std::thread([host, promise]() {
        struct addrinfo hints{};
        hints.ai_family = AF_INET;
        hints.ai_socktype = SOCK_DGRAM;
        struct addrinfo* res = nullptr;
        std::string ip;
        if (getaddrinfo(host.c_str(), nullptr, &hints, &res) == 0 && res) {
            char buf[INET_ADDRSTRLEN] = {0};
            auto* sa = reinterpret_cast<struct sockaddr_in*>(res->ai_addr);
            if (inet_ntop(AF_INET, &sa->sin_addr, buf, sizeof(buf))) ip = buf;
        }
        if (res) freeaddrinfo(res);
        promise->set_value(ip);
    }).detach();
    if (fut.wait_for(std::chrono::milliseconds(timeout_ms)) == std::future_status::ready) return fut.get();
    return "";
}

std::string replace_first(const std::string& s, const std::string& from, const std::string& to) {
    const size_t pos = s.find(from);
    return pos == std::string::npos ? s : s.substr(0, pos) + to + s.substr(pos + from.size());
}

// A FAILED/DISCONNECTED peer is torn down only after this many consecutive 200 ms polls (~15 s) — long
// enough for webrtcbin to recover a transient blip via ICE restart, short enough to free a dead peer.
constexpr int kTeardownGracePolls = 75;

// A peer that never reaches CONNECTED within this window (failed ICE / abandoned handshake) is
// released, so a START that never completes can't leak its transport (and pin the encoder on).
constexpr double kConnectTimeoutS = 15.0;

// ---- WebRTC "playout-delay" RTP header extension -----------------------------------------------
// Caps the receiver's de-jitter buffer. GStreamer < 1.24 ships no built-in element for this URI, so we
// implement it as a minimal GstRTPHeaderExtension subclass and add it to each payloader; the matching
// a=extmap is emitted by webrtcbin from the transport appsrc caps. Wire format (WebRTC experiment):
// 3 bytes = MIN delay (12 bits) | MAX delay (12 bits), 10 ms units.
#define MARS_PLAYOUT_DELAY_URI "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay"
constexpr guint kPlayoutDelayExtId = 14;

struct MarsPlayoutDelayExt {
    GstRTPHeaderExtension parent;
    guint min_delay_ms;
    guint max_delay_ms;
};
struct MarsPlayoutDelayExtClass {
    GstRTPHeaderExtensionClass parent_class;
};

G_DEFINE_TYPE(MarsPlayoutDelayExt, mars_playout_delay_ext, GST_TYPE_RTP_HEADER_EXTENSION)

GstRTPHeaderExtensionFlags mars_playout_delay_supported_flags(GstRTPHeaderExtension*) {
    return static_cast<GstRTPHeaderExtensionFlags>(GST_RTP_HEADER_EXTENSION_ONE_BYTE |
                                                   GST_RTP_HEADER_EXTENSION_TWO_BYTE);
}

gsize mars_playout_delay_max_size(GstRTPHeaderExtension*, const GstBuffer*) { return 3; }

gssize mars_playout_delay_write(GstRTPHeaderExtension* ext, const GstBuffer*, GstRTPHeaderExtensionFlags,
                                GstBuffer*, guint8* data, gsize size) {
    if (size < 3) {
        return -1;
    }
    auto* self = reinterpret_cast<MarsPlayoutDelayExt*>(ext);
    const guint min_units = (self->min_delay_ms / 10) & 0xFFF;  // 12-bit, 10 ms units
    const guint max_units = (self->max_delay_ms / 10) & 0xFFF;
    data[0] = static_cast<guint8>(min_units >> 4);
    data[1] = static_cast<guint8>(((min_units & 0xF) << 4) | ((max_units >> 8) & 0xF));
    data[2] = static_cast<guint8>(max_units & 0xFF);
    return 3;
}

void mars_playout_delay_ext_class_init(MarsPlayoutDelayExtClass* klass) {
    GstRTPHeaderExtensionClass* ext_class = GST_RTP_HEADER_EXTENSION_CLASS(klass);
    ext_class->get_supported_flags = mars_playout_delay_supported_flags;
    ext_class->get_max_size = mars_playout_delay_max_size;
    ext_class->write = mars_playout_delay_write;
    gst_rtp_header_extension_class_set_uri(ext_class, MARS_PLAYOUT_DELAY_URI);
    gst_element_class_set_metadata(GST_ELEMENT_CLASS(klass), "Playout delay RTP header extension",
                                   GST_RTP_HDREXT_ELEMENT_CLASS, "WebRTC playout-delay header extension", "mars_cam");
}

void mars_playout_delay_ext_init(MarsPlayoutDelayExt* self) {
    self->min_delay_ms = 0;
    self->max_delay_ms = 0;
}

GstRTPHeaderExtension* make_playout_delay_ext(guint ext_id, guint min_ms, guint max_ms) {
    auto* self = static_cast<MarsPlayoutDelayExt*>(g_object_new(mars_playout_delay_ext_get_type(), nullptr));
    self->min_delay_ms = min_ms;
    self->max_delay_ms = max_ms;
    gst_rtp_header_extension_set_id(GST_RTP_HEADER_EXTENSION(self), ext_id);
    return GST_RTP_HEADER_EXTENSION(self);
}

// Context handed to the async create-offer callback. Holds a ref to the peer's webrtcbin (so it
// survives a concurrent teardown) and a copy of the peer's generation token (a shared_ptr, so it stays
// readable even after the Peer is freed): if the peer was torn down/replaced, the generation no longer
// matches and the stale offer is dropped instead of being applied to a vanished connection.
struct OfferContext {
    mars_cam::WebRTCStreamer* self;
    GstElement* webrtc;  // owns a ref, released in offer_context_free
    std::shared_ptr<std::atomic<uint64_t>> gen;
    uint64_t gen_value;
    std::string client_id;
};

void offer_context_free(gpointer data) {
    auto* ctx = static_cast<OfferContext*>(data);
    if (ctx->webrtc) {
        gst_object_unref(ctx->webrtc);
    }
    delete ctx;
}
}  // namespace

WebRTCStreamer::WebRTCStreamer(const rclcpp::NodeOptions& options)
    : Node("webrtc_streamer", options), camera_qos_(rclcpp::QoS(1).best_effort()) {
    gst_init(nullptr, nullptr);

    this->declare_parameter("use_compressed_images", false);
    this->declare_parameter("enable_audio", true);
    this->declare_parameter("audio_source_element", "alsasrc");
    this->declare_parameter("audio_capture_device", "");
    this->declare_parameter("playout_min_delay_ms", 0);
    this->declare_parameter("playout_max_delay_ms", 40);
    this->declare_parameter("rtcp_inactivity_timeout_s", 5.0);

    use_compressed_images_ = this->get_parameter("use_compressed_images").as_bool();
    enable_audio_ = this->get_parameter("enable_audio").as_bool();
    audio_source_element_ = this->get_parameter("audio_source_element").as_string();
    audio_capture_device_ = this->get_parameter("audio_capture_device").as_string();
    playout_min_delay_ms_ = static_cast<guint>(this->get_parameter("playout_min_delay_ms").as_int());
    playout_max_delay_ms_ = static_cast<guint>(this->get_parameter("playout_max_delay_ms").as_int());
    rtcp_inactivity_timeout_s_ = this->get_parameter("rtcp_inactivity_timeout_s").as_double();
    rtcp_timeout_cb_ = this->add_on_set_parameters_callback(
        [this](const std::vector<rclcpp::Parameter>& params) {
            rcl_interfaces::msg::SetParametersResult result;
            result.successful = true;
            for (const auto& p : params) {
                if (p.get_name() != "rtcp_inactivity_timeout_s") {
                    continue;
                }
                const double v = p.as_double();
                if (v <= 0.0) {
                    result.successful = false;
                    result.reason = "rtcp_inactivity_timeout_s must be > 0";
                } else {
                    rtcp_inactivity_timeout_s_ = v;
                    RCLCPP_INFO(this->get_logger(), "rtcp_inactivity_timeout_s set to %.1f s", v);
                }
            }
            return result;
        });

    // Publishers: bare topics serve the legacy single peer (raw SDP); the *_id variants carry a
    // client_id envelope so multiple peers can negotiate independently on the same topics.
    offer_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/offer", 10);
    offer_id_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/offer_id", 10);
    ice_out_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/ice_out", 10);
    ice_out_id_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/ice_out_id", 10);
    active_streams_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/active_streams", 10);

    start_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/start", 10, std::bind(&WebRTCStreamer::on_start, this, std::placeholders::_1));
    answer_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/answer", 10, std::bind(&WebRTCStreamer::on_answer, this, std::placeholders::_1));
    answer_id_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/answer_id", 10, std::bind(&WebRTCStreamer::on_answer_id, this, std::placeholders::_1));
    ice_in_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/ice_in", 10, std::bind(&WebRTCStreamer::on_ice_in, this, std::placeholders::_1));
    ice_in_id_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/ice_in_id", 10, std::bind(&WebRTCStreamer::on_ice_in_id, this, std::placeholders::_1));

    // The encoders run for the node's lifetime; only the per-peer transport churns. Configure the camera
    // set, build them now — the camera callbacks gate the actual CPU, and the topics are subscribed lazily
    // (reconcile_subscriptions on the first peer), so an idle node receives no camera frames.
    configure_cameras();
    if (!build_encode_pipeline()) {
        RCLCPP_FATAL(this->get_logger(), "Failed to build the persistent encode pipeline");
        throw std::runtime_error("encode pipeline build failed");
    }

    health_timer_ =
        this->create_wall_timer(std::chrono::milliseconds(200), std::bind(&WebRTCStreamer::poll_pipeline_health, this));
    prev_status_time_ = std::chrono::steady_clock::now();
    status_timer_ =
        this->create_wall_timer(std::chrono::seconds(2), std::bind(&WebRTCStreamer::publish_status, this));

    RCLCPP_INFO(this->get_logger(), "WebRTC Streamer ready (%zu cameras, source: %s, compressed: %s)",
                cameras_.size(), current_source_.c_str(), use_compressed_images_ ? "true" : "false");
    RCLCPP_INFO(this->get_logger(), "  Mic audio: %s", enable_audio_ ? "enabled (opt-in per peer)" : "disabled");
    RCLCPP_INFO(this->get_logger(), "  RTCP-inactivity teardown: %.1f s", rtcp_inactivity_timeout_s_);
}

void WebRTCStreamer::configure_cameras() {
    // `cameras` lists the camera names (m-line order). Each gets per-camera params:
    //   live_<name>_camera_topic, replay_<name>_camera_topic, <name>_fps
    // The built-in `main`/`arm` keep their existing topic/fps defaults (so existing launches are
    // unchanged); any other name must supply its own topics. PT + SSRC are assigned by index.
    static const std::map<std::string, std::tuple<std::string, std::string, int>> kDefaults = {
        {"main", {"/mars/main_camera/left/image_raw", "/brain/recorder/replay/main_camera/left/image_raw", 30}},
        {"arm", {"/mars/arm/image_raw", "/brain/recorder/replay/arm_camera/image_raw", 15}},
    };
    const auto names = this->declare_parameter<std::vector<std::string>>("cameras", {"main", "arm"});
    for (const auto& name : names) {
        std::string def_live, def_replay;
        int def_fps = 30;
        if (auto it = kDefaults.find(name); it != kDefaults.end()) {
            std::tie(def_live, def_replay, def_fps) = it->second;
        }
        auto cam = std::make_unique<CameraEncoder>();
        cam->name = name;
        cam->live_topic = this->declare_parameter<std::string>("live_" + name + "_camera_topic", def_live);
        cam->replay_topic = this->declare_parameter<std::string>("replay_" + name + "_camera_topic", def_replay);
        cam->fps = static_cast<int>(this->declare_parameter<int>(name + "_fps", def_fps));
        cam->pt = cam_pt_for_index(cameras_.size());
        cam->ssrc = cam_ssrc_for_index(cameras_.size());
        cam->owner = this;
        if (cam->live_topic.empty()) {
            RCLCPP_WARN(this->get_logger(), "Camera '%s' has no live topic configured; skipping it", name.c_str());
            continue;
        }
        RCLCPP_INFO(this->get_logger(), "  Camera[%zu] '%s': pt=%d ssrc=0x%08X fps=%d live=%s", cameras_.size(),
                    name.c_str(), cam->pt, cam->ssrc, cam->fps, cam->live_topic.c_str());
        cameras_.push_back(std::move(cam));
    }
    if (cameras_.empty()) {
        RCLCPP_FATAL(this->get_logger(), "No cameras configured (the `cameras` parameter is empty)");
        throw std::runtime_error("no cameras configured");
    }
}

CameraEncoder* WebRTCStreamer::find_camera(const std::string& name) {
    for (auto& cam : cameras_) {
        if (cam->name == name) {
            return cam.get();
        }
    }
    return nullptr;
}

WebRTCStreamer::~WebRTCStreamer() {
    health_timer_.reset();
    status_timer_.reset();
    destroy_subscriptions();
    {
        std::lock_guard<std::mutex> lock(peers_mutex_);
        std::vector<std::string> ids;
        for (auto& kv : peers_) {
            ids.push_back(kv.first);
        }
        for (const auto& id : ids) {
            destroy_peer(id);
        }
    }
    // Tear down the persistent encode pipeline.
    for (auto& cam : cameras_) {
        if (cam->pool) {
            gst_buffer_pool_set_active(cam->pool, FALSE);
            gst_object_unref(cam->pool);
        }
        if (cam->appsrc) gst_object_unref(cam->appsrc);
        if (cam->sink) gst_object_unref(cam->sink);
    }
    if (encode_pipeline_) {
        gst_element_set_state(encode_pipeline_, GST_STATE_NULL);
        gst_object_unref(encode_pipeline_);
    }
}

// =============================================================================
// Persistent encode pipeline
// =============================================================================

std::string WebRTCStreamer::video_encode_branch(const std::string& name, int pt, int fps, guint ssrc) const {
    // appsrc -> encoder -> payloader -> appsink. The appsink is the fan-out tap: every connected peer's
    // transport appsrc is fed from here, so each camera is encoded exactly once regardless of peer count.
    return "appsrc name=src_" + name +
           " is-live=true format=time caps=video/x-raw,format=BGR,width=640,height=480,framerate=" +
           std::to_string(fps) +
           "/1 ! "
           "queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 ! "
           "videoconvert ! "
           "vp8enc deadline=1 target-bitrate=2000000 cpu-used=4 error-resilient=partitions keyframe-max-dist=30 "
           "end-usage=cbr buffer-size=600 buffer-initial-size=400 buffer-optimal-size=500 ! "
           "rtpvp8pay name=pay_" +
           name + " pt=" + std::to_string(pt) + " ssrc=" + std::to_string(ssrc) +
           " ! "
           "appsink name=sink_" +
           name + " emit-signals=true sync=false max-buffers=2 drop=true ";
}

void WebRTCStreamer::attach_playout_delay_extension(const std::string& cam) {
    // Add the playout-delay extension to the payloader so it writes the 3 bytes into every RTP packet.
    // The matching a=extmap is emitted per peer from the transport appsrc caps (set in
    // create_peer_transport), keyed by the same extmap id.
    const std::string payloader = "pay_" + cam;
    GstElement* pay = gst_bin_get_by_name(GST_BIN(encode_pipeline_), payloader.c_str());
    if (!pay) {
        RCLCPP_WARN(this->get_logger(), "Missing %s; playout-delay extension not applied", payloader.c_str());
        return;
    }
    GstRTPHeaderExtension* ext = make_playout_delay_ext(kPlayoutDelayExtId, playout_min_delay_ms_, playout_max_delay_ms_);
    g_signal_emit_by_name(pay, "add-extension", ext);  // transfer full: payloader owns ext now
    gst_object_unref(pay);
}

bool WebRTCStreamer::build_encode_pipeline() {
    std::string desc;
    for (const auto& cam : cameras_) {
        desc += video_encode_branch(cam->name, cam->pt, cam->fps, cam->ssrc);
    }
    GError* error = nullptr;
    encode_pipeline_ = gst_parse_launch(desc.c_str(), &error);
    if (error) {
        RCLCPP_ERROR(this->get_logger(), "Failed to create encode pipeline: %s", error->message);
        g_error_free(error);
        if (encode_pipeline_) {
            gst_object_unref(encode_pipeline_);
            encode_pipeline_ = nullptr;
        }
        return false;
    }

    for (auto& cam : cameras_) {
        cam->appsrc = gst_bin_get_by_name(GST_BIN(encode_pipeline_), ("src_" + cam->name).c_str());
        cam->sink = gst_bin_get_by_name(GST_BIN(encode_pipeline_), ("sink_" + cam->name).c_str());
        if (!cam->appsrc || !cam->sink) {
            RCLCPP_ERROR(this->get_logger(), "Encode pipeline missing elements for camera '%s'", cam->name.c_str());
            return false;
        }
        g_object_set(cam->appsrc, "format", GST_FORMAT_TIME, "do-timestamp", TRUE, "is-live", TRUE, "block", FALSE,
                     "max-bytes", 2 * 640 * 480 * 3, nullptr);
        cam->pool = create_frame_pool(640, 480, 3);
        attach_playout_delay_extension(cam->name);
        // user_data = the CameraEncoder* so the one static handler knows which camera fired.
        g_signal_connect(cam->sink, "new-sample", G_CALLBACK(on_sample), cam.get());
    }

    if (gst_element_set_state(encode_pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
        RCLCPP_ERROR(this->get_logger(), "Encode pipeline failed to reach PLAYING");
        return false;
    }
    std::string names;
    for (const auto& cam : cameras_) names += (names.empty() ? "" : "+") + cam->name;
    RCLCPP_INFO(this->get_logger(), "Persistent encode pipeline PLAYING (%s, idle until a peer connects)",
                names.c_str());
    return true;
}

void WebRTCStreamer::force_keyframe(const std::string& cam) {
    CameraEncoder* c = find_camera(cam);
    if (!c || !c->sink) {
        return;
    }
    GstElement* sink = c->sink;
    GstPad* sinkpad = gst_element_get_static_pad(sink, "sink");
    if (!sinkpad) {
        return;
    }
    // Upstream force-key-unit event (same structure GstVideo builds) so vp8enc emits an IDR a fresh or
    // just-resumed peer can decode immediately, rather than waiting up to keyframe-max-dist frames. Send
    // it on the peer (the payloader's src pad) so it travels upstream from there — sending an upstream
    // event straight at a sink pad warns about "wrong direction".
    GstPad* peer = gst_pad_get_peer(sinkpad);
    if (peer) {
        GstStructure* s = gst_structure_new("GstForceKeyUnit", "all-headers", G_TYPE_BOOLEAN, TRUE, "count",
                                            G_TYPE_UINT, static_cast<guint>(0), nullptr);
        gst_pad_send_event(peer, gst_event_new_custom(GST_EVENT_CUSTOM_UPSTREAM, s));
        gst_object_unref(peer);
    }
    gst_object_unref(sinkpad);
}

// =============================================================================
// Fan-out: encoded RTP -> every peer wanting this camera
// =============================================================================

GstFlowReturn WebRTCStreamer::on_sample(GstElement* appsink, gpointer user_data) {
    auto* cam = static_cast<CameraEncoder*>(user_data);  // wired in build_encode_pipeline
    cam->owner->fan_out_sample(appsink, cam->name);
    return GST_FLOW_OK;
}

void WebRTCStreamer::fan_out_sample(GstElement* appsink, const std::string& cam) {
    GstSample* sample = gst_app_sink_pull_sample(GST_APP_SINK(appsink));
    if (!sample) {
        return;
    }
    GstBuffer* buffer = gst_sample_get_buffer(sample);
    if (buffer) {
        // Collect (and ref) this camera's per-peer appsrcs under a BRIEF lock, then release it before the
        // heap copy + push-buffer for each. Otherwise fan-out at 30 fps x N peers holds peers_mutex_ for
        // tens of µs/frame and delays the answer/ICE/health callbacks that contend on it. The refs keep
        // the appsrcs alive if a peer is torn down between the unlock and the push (a push to a now-NULL
        // appsrc just returns FLUSHING — harmless).
        std::vector<GstElement*> targets;
        {
            std::lock_guard<std::mutex> lock(peers_mutex_);
            for (auto& kv : peers_) {
                auto it = kv.second->rtp.find(cam);
                if (it != kv.second->rtp.end() && it->second && wants(kv.second->active, cam)) {
                    targets.push_back(GST_ELEMENT(gst_object_ref(it->second)));
                }
            }
        }
        for (GstElement* src : targets) {
            // Push a WRITABLE copy with its timestamps cleared, not a shared ref: the buffer carries the
            // encode pipeline's PTS (a different, future-dated timebase), and a shared/non-writable buffer
            // can't be re-stamped, so webrtcbin would hold every frame after the first burst. The copy lets
            // the transport appsrc's do-timestamp assign this pipeline's running-time.
            GstBuffer* out = gst_buffer_copy(buffer);
            GST_BUFFER_PTS(out) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DTS(out) = GST_CLOCK_TIME_NONE;
            GstFlowReturn ret;
            g_signal_emit_by_name(src, "push-buffer", out, &ret);  // takes ownership of the copy
            gst_object_unref(src);
        }
    }
    gst_sample_unref(sample);
}

// =============================================================================
// Camera subscriptions + frame ingest (gated by per-camera want-count)
// =============================================================================

void WebRTCStreamer::destroy_subscriptions() {
    for (auto& cam : cameras_) {
        cam->sub.reset();
    }
}

void WebRTCStreamer::set_camera_subscribed(CameraEncoder* cam, bool subscribed) {
    const bool have = static_cast<bool>(cam->sub);
    if (subscribed == have) {
        return;  // already in the desired state
    }
    if (!subscribed) {
        cam->sub.reset();
        RCLCPP_INFO(this->get_logger(), "Unsubscribed %s camera (no peer wants it)", cam->name.c_str());
        return;
    }
    const bool replay = current_source_ == "replay";
    const std::string topic = replay ? cam->replay_topic : cam->live_topic;
    if (use_compressed_images_) {
        cam->sub = this->create_subscription<sensor_msgs::msg::CompressedImage>(
            topic + "/compressed", camera_qos_,
            [this, cam](const sensor_msgs::msg::CompressedImage::SharedPtr msg) { on_image_compressed(cam, msg); });
    } else {
        cam->sub = this->create_subscription<sensor_msgs::msg::Image>(
            topic, camera_qos_, [this, cam](const sensor_msgs::msg::Image::SharedPtr msg) { on_image_raw(cam, msg); });
    }
    RCLCPP_INFO(this->get_logger(), "Subscribed %s camera: %s", cam->name.c_str(), topic.c_str());
}

void WebRTCStreamer::reconcile_subscriptions() {
    // A camera is worth receiving only if some connected peer negotiated it. Keying on negotiated (not
    // active) cameras keeps the subs alive for the whole session so a stream switch stays instant, while
    // an idle node (no peers) drops every camera sub and receives nothing. Caller holds peers_mutex_.
    for (auto& cam : cameras_) {
        bool need = false;
        for (auto& kv : peers_) {
            if (wants(kv.second->videos, cam->name)) {
                need = true;
                break;
            }
        }
        set_camera_subscribed(cam.get(), need);
    }
}

cv::Mat WebRTCStreamer::process_raw_image(const sensor_msgs::msg::Image::SharedPtr& msg, int target_width,
                                          int target_height) {
    if (!msg || msg->data.empty() || msg->height == 0 || msg->width == 0) {
        return cv::Mat();
    }
    int cv_type = CV_8UC3;
    int conversion_code = -1;
    if (msg->encoding == "rgb8") {
        conversion_code = cv::COLOR_RGB2BGR;
    } else if (msg->encoding == "bgr8") {
        conversion_code = -1;
    } else if (msg->encoding == "mono8") {
        cv_type = CV_8UC1;
        conversion_code = cv::COLOR_GRAY2BGR;
    } else if (msg->encoding == "rgba8") {
        cv_type = CV_8UC4;
        conversion_code = cv::COLOR_RGBA2BGR;
    } else if (msg->encoding == "bgra8") {
        cv_type = CV_8UC4;
        conversion_code = cv::COLOR_BGRA2BGR;
    } else {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                             "Unsupported image encoding: %s, assuming bgr8", msg->encoding.c_str());
        conversion_code = -1;
    }

    cv::Mat img(msg->height, msg->width, cv_type, const_cast<uint8_t*>(msg->data.data()), msg->step);
    if (img.empty()) {
        return cv::Mat();
    }
    bool needs_resize = (img.rows != target_height || img.cols != target_width);
    if (conversion_code < 0 && !needs_resize) {
        return img;  // non-owning view; msg stays alive until memcpy into the GstBuffer
    }
    cv::Mat result;
    if (conversion_code >= 0) {
        cv::cvtColor(img, result, conversion_code);
        if (needs_resize) {
            cv::resize(result, result, cv::Size(target_width, target_height));
        }
    } else {
        cv::resize(img, result, cv::Size(target_width, target_height));
    }
    return result;
}

cv::Mat WebRTCStreamer::process_compressed_image(const sensor_msgs::msg::CompressedImage::SharedPtr& msg,
                                                 int target_width, int target_height) {
    if (!msg || msg->data.empty()) {
        return cv::Mat();
    }
    cv::Mat img = cv::imdecode(cv::Mat(msg->data), cv::IMREAD_COLOR);
    if (img.empty()) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                             "Failed to decode compressed image (format: %s)", msg->format.c_str());
        return cv::Mat();
    }
    if (img.rows != target_height || img.cols != target_width) {
        cv::resize(img, img, cv::Size(target_width, target_height));
    }
    return img;
}

GstBufferPool* WebRTCStreamer::create_frame_pool(int width, int height, int channels) {
    gsize frame_size = width * height * channels;
    GstBufferPool* pool = gst_buffer_pool_new();
    GstStructure* config = gst_buffer_pool_get_config(pool);
    gst_buffer_pool_config_set_params(config, nullptr, frame_size, 2, 4);
    if (!gst_buffer_pool_set_config(pool, config)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to configure buffer pool");
        gst_object_unref(pool);
        return nullptr;
    }
    if (!gst_buffer_pool_set_active(pool, TRUE)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to activate buffer pool");
        gst_object_unref(pool);
        return nullptr;
    }
    return pool;
}

void WebRTCStreamer::push_frame(CameraEncoder* cam, const cv::Mat& frame) {
    if (!cam->appsrc || frame.empty() || !cam->pool) {
        return;
    }
    GstBuffer* buffer = nullptr;
    if (gst_buffer_pool_acquire_buffer(cam->pool, &buffer, nullptr) != GST_FLOW_OK || !buffer) {
        return;
    }
    GstMapInfo map;
    gst_buffer_map(buffer, &map, GST_MAP_WRITE);
    memcpy(map.data, frame.data, frame.total() * frame.elemSize());
    gst_buffer_unmap(buffer, &map);

    GstFlowReturn ret;
    g_signal_emit_by_name(cam->appsrc, "push-buffer", buffer, &ret);
    gst_buffer_unref(buffer);  // returns to pool
    cam->frames.fetch_add(1, std::memory_order_relaxed);
}

void WebRTCStreamer::on_image_raw(CameraEncoder* cam, const sensor_msgs::msg::Image::SharedPtr& msg) {
    if (cam->want.load(std::memory_order_relaxed) == 0) {
        return;  // no peer wants this camera -> skip all encode work (flat memory, zero idle CPU)
    }
    cv::Mat img = process_raw_image(msg, 640, 480);
    if (!img.empty()) {
        push_frame(cam, img);
    }
}

void WebRTCStreamer::on_image_compressed(CameraEncoder* cam, const sensor_msgs::msg::CompressedImage::SharedPtr& msg) {
    if (cam->want.load(std::memory_order_relaxed) == 0) {
        return;
    }
    cv::Mat img = process_compressed_image(msg, 640, 480);
    if (!img.empty()) {
        push_frame(cam, img);
    }
}

// =============================================================================
// Per-peer transport
// =============================================================================

std::string WebRTCStreamer::build_transport_description(const std::vector<std::string>& videos,
                                                        bool& with_audio) const {
    // Declare the branches but DON'T link them to webrtcbin here: the RTP caps (incl. the extmap) are
    // set on each appsrc programmatically and the sink pads are requested/linked in order afterwards,
    // so webrtcbin sees the right caps at link time (linking with empty caps makes it collapse both
    // pads onto one transceiver -> the _create_offer_task seen_transceivers assertion).
    std::string desc = "webrtcbin name=webrtc bundle-policy=max-bundle ";
    for (const auto& v : videos) {
        desc += "appsrc name=rtp_" + v + " is-live=true format=time do-timestamp=false ";
    }

    if (with_audio) {
        const bool valid_element = !audio_source_element_.empty() &&
                                   std::all_of(audio_source_element_.begin(), audio_source_element_.end(),
                                               [](unsigned char c) { return std::isalnum(c) || c == '-' || c == '_'; });
        if (!valid_element) {
            RCLCPP_ERROR(this->get_logger(), "audio_source_element '%s' is not a plain element name; video only",
                         audio_source_element_.c_str());
            with_audio = false;
            return desc;
        }
        std::string src = audio_source_element_;
        if (!audio_capture_device_.empty()) {
            const bool valid_device =
                std::all_of(audio_capture_device_.begin(), audio_capture_device_.end(), [](unsigned char c) {
                    return std::isalnum(c) || c == ':' || c == ',' || c == '.' || c == '=' || c == '-' || c == '_' ||
                           c == '/';
                });
            if (!valid_device) {
                RCLCPP_ERROR(this->get_logger(), "audio_capture_device '%s' has unexpected chars; video only",
                             audio_capture_device_.c_str());
                with_audio = false;
                return desc;
            }
            src += " device=\"" + audio_capture_device_ + "\"";
        }
        desc += " " + src +
                " do-timestamp=true ! "
                "queue leaky=downstream max-size-buffers=10 max-size-time=0 max-size-bytes=0 ! "
                "audioconvert ! audioresample ! "
                "audio/x-raw,rate=48000,channels=1 ! "
                "opusenc bitrate=24000 audio-type=voice ! "
                "rtpopuspay name=pay_audio pt=98 ! "
                "capsfilter name=caps_audio "
                "caps=application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,encoding-params=(string)2,"
                "payload=98 ";
    }
    return desc;
}

Peer* WebRTCStreamer::create_peer_transport(const std::string& client_id, const std::vector<std::string>& negotiated,
                                            const std::vector<std::string>& active, bool with_audio) {
    destroy_peer(client_id);  // replace any existing peer with this id (re-START)

    std::string desc = build_transport_description(negotiated, with_audio);
    GError* error = nullptr;
    GstElement* pipeline = gst_parse_launch(desc.c_str(), &error);
    if (error) {
        RCLCPP_ERROR(this->get_logger(), "Failed to create transport pipeline (audio=%s): %s",
                     with_audio ? "on" : "off", error->message);
        g_error_free(error);
        if (pipeline) {
            gst_object_unref(pipeline);
        }
        return nullptr;
    }

    auto peer = std::make_unique<Peer>();
    peer->client_id = client_id;
    peer->pipeline = pipeline;
    peer->videos = negotiated;
    peer->active = active;
    peer->with_audio = with_audio;
    peer->created_ns = std::chrono::steady_clock::now().time_since_epoch().count();
    peer->webrtc = gst_bin_get_by_name(GST_BIN(pipeline), "webrtc");
    if (!peer->webrtc) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing webrtcbin");
        gst_element_set_state(pipeline, GST_STATE_NULL);
        gst_object_unref(pipeline);
        return nullptr;
    }

    // Configure each RTP appsrc: caps (incl. the extmap so webrtcbin emits a=extmap), drop-old on
    // congestion so one slow peer can't backpressure the shared encoders.
    const std::string extmap_field = "extmap-" + std::to_string(kPlayoutDelayExtId);
    bool ok = true;
    for (const auto& v : negotiated) {
        CameraEncoder* c = find_camera(v);
        GstElement* src = c ? gst_bin_get_by_name(GST_BIN(pipeline), ("rtp_" + v).c_str()) : nullptr;
        if (!c || !src) {
            if (src) gst_object_unref(src);
            ok = false;
            break;
        }
        GstCaps* caps = gst_caps_new_simple("application/x-rtp", "media", G_TYPE_STRING, "video", "encoding-name",
                                            G_TYPE_STRING, "VP8", "clock-rate", G_TYPE_INT, 90000, "payload", G_TYPE_INT,
                                            c->pt, "ssrc", G_TYPE_UINT, c->ssrc, nullptr);
        gst_caps_set_simple(caps, extmap_field.c_str(), G_TYPE_STRING, MARS_PLAYOUT_DELAY_URI, nullptr);
        // do-timestamp=TRUE: re-stamp each forwarded RTP buffer with this (transport) pipeline's
        // running-time on arrival. The encode pipeline is a SEPARATE pipeline with its own base-time, so
        // its PTS look like they're in the future here and webrtcbin would hold/never send them. The RTP
        // header timestamps the receiver uses for playback are written by the payloader and untouched.
        g_object_set(src, "caps", caps, "is-live", TRUE, "format", GST_FORMAT_TIME, "do-timestamp", TRUE, "block",
                     FALSE, "leaky-type", 2 /* downstream */, "max-bytes", 2 * 1024 * 1024, nullptr);
        gst_caps_unref(caps);

        // Caps are set; NOW request the next webrtcbin sink pad and link, so the transceiver is built
        // from the real VP8 caps (in m-line order: sink_0, sink_1, ...).
        GstPad* srcpad = gst_element_get_static_pad(src, "src");
        GstPad* sinkpad = gst_element_request_pad_simple(peer->webrtc, "sink_%u");
        const bool linked = srcpad && sinkpad && gst_pad_link(srcpad, sinkpad) == GST_PAD_LINK_OK;
        if (srcpad) gst_object_unref(srcpad);
        if (sinkpad) gst_object_unref(sinkpad);
        if (!linked) {
            RCLCPP_ERROR(this->get_logger(), "Failed to link rtp_%s to webrtcbin", v.c_str());
            gst_object_unref(src);
            ok = false;
            break;
        }
        peer->rtp[v] = src;  // keep the ref (camera name -> transport appsrc)
    }
    // Link the audio branch (caps already fixed by its capsfilter) onto the next sink in order.
    if (ok && with_audio) {
        if (GstElement* acaps = gst_bin_get_by_name(GST_BIN(pipeline), "caps_audio")) {
            GstPad* srcpad = gst_element_get_static_pad(acaps, "src");
            GstPad* sinkpad = gst_element_request_pad_simple(peer->webrtc, "sink_%u");
            if (!srcpad || !sinkpad || gst_pad_link(srcpad, sinkpad) != GST_PAD_LINK_OK) {
                RCLCPP_WARN(this->get_logger(), "Failed to link audio to webrtcbin; continuing video-only");
            }
            if (srcpad) gst_object_unref(srcpad);
            if (sinkpad) gst_object_unref(sinkpad);
            gst_object_unref(acaps);
        }
    }
    if (!ok) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing/failed an rtp appsrc");
        gst_element_set_state(pipeline, GST_STATE_NULL);
        for (auto& kv : peer->rtp) gst_object_unref(kv.second);
        gst_object_unref(peer->webrtc);
        gst_object_unref(pipeline);
        return nullptr;
    }

    // Tag the webrtcbin with its client_id (ICE-candidate routing) and a copy of its generation token,
    // so the on-negotiation-needed handler can build the offer context lock-free off the element alone.
    g_object_set_data_full(G_OBJECT(peer->webrtc), "client_id", g_strdup(client_id.c_str()), g_free);
    g_object_set_data_full(G_OBJECT(peer->webrtc), "mars_gen",
                           new std::shared_ptr<std::atomic<uint64_t>>(peer->generation),
                           [](gpointer p) { delete static_cast<std::shared_ptr<std::atomic<uint64_t>>*>(p); });
    g_signal_connect(peer->webrtc, "on-ice-candidate", G_CALLBACK(on_ice_candidate), this);
    g_signal_connect(peer->webrtc, "notify::connection-state", G_CALLBACK(on_connection_state_changed), this);

    if (GstObject* ice = nullptr; (g_object_get(peer->webrtc, "ice-agent", &ice, nullptr), ice)) {
        g_object_set(ice, "ice-tcp", FALSE, nullptr);  // UDP-only media: fewer candidate pairs to check
        // Cap the STUN retransmit budget on the underlying NiceAgent so a dead pair (e.g. the tailscale
        // candidate when the client is on the LAN) gives up in ~0.7 s instead of ~5 s, rather than holding
        // up nomination of the working pair. A real LAN/tailscale pair answers on the first check, well
        // inside this budget, so connectivity is unaffected. (The big connect-latency win is the mDNS
        // candidate handling in prepare_ice_candidate, not this.)
        if (GObject* nice = nullptr; (g_object_get(ice, "agent", &nice, nullptr), nice)) {
            g_object_set(nice, "stun-initial-timeout", static_cast<guint>(100), "stun-max-retransmissions",
                         static_cast<guint>(2), nullptr);
            g_object_unref(nice);
        }
        gst_object_unref(ice);
    }
    // Let webrtcbin tell us when the transceivers are ready instead of racing create-offer right after
    // PLAYING (that race produced empty/partial offers -> the _connect_input_stream crash on the answer).
    g_signal_connect(peer->webrtc, "on-negotiation-needed", G_CALLBACK(on_negotiation_needed), this);

    if (with_audio) {
        if (GstElement* pay = gst_bin_get_by_name(GST_BIN(pipeline), "pay_audio")) {
            if (GstPad* pad = gst_element_get_static_pad(pay, "src")) {
                gst_pad_add_probe(pad,
                                  static_cast<GstPadProbeType>(GST_PAD_PROBE_TYPE_BUFFER | GST_PAD_PROBE_TYPE_BUFFER_LIST),
                                  on_audio_buffer, peer.get(), nullptr);
                gst_object_unref(pad);
                peer->audio_probe_installed = true;
            }
            gst_object_unref(pay);
        }
    }

    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_ASYNC) {
        ret = gst_element_get_state(pipeline, nullptr, nullptr, 3 * GST_SECOND);
    }
    if (ret == GST_STATE_CHANGE_FAILURE) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline failed to reach PLAYING (audio=%s)",
                     with_audio ? "on" : "off");
        gst_element_set_state(pipeline, GST_STATE_NULL);
        for (auto& kv : peer->rtp) gst_object_unref(kv.second);
        gst_object_unref(peer->webrtc);
        gst_object_unref(pipeline);
        return nullptr;
    }

    Peer* raw = peer.get();
    peers_[client_id] = std::move(peer);
    // want-count gates the encoders; count only ACTIVE (pushed) cameras, not merely negotiated ones, so a
    // peer that negotiated several but is viewing one doesn't pin the others' encoders on.
    for (const auto& v : active) {
        if (CameraEncoder* c = find_camera(v)) c->want.fetch_add(1, std::memory_order_relaxed);
    }
    reconcile_subscriptions();  // this peer just negotiated its cameras — make sure they're subscribed

    // The offer is created from on-negotiation-needed (fires once the transceivers are set up).
    RCLCPP_INFO(this->get_logger(), "Peer '%s' transport PLAYING (negotiated=%zu, active=%zu, audio=%s)",
                client_id.empty() ? "(default)" : client_id.c_str(), negotiated.size(), active.size(),
                with_audio ? "on" : "off");
    return raw;
}

void WebRTCStreamer::update_peer_active(Peer* peer, const std::vector<std::string>& active) {
    // Toggle the pushed cameras on a peer whose transceivers are already negotiated — no offer/answer, no
    // ICE, so a stream switch is instant instead of a full reconnect. Only cameras the peer negotiated can
    // be enabled; anything else would need renegotiation and is ignored here.
    std::vector<std::string> next;
    for (const auto& v : active) {
        if (wants(peer->videos, v) && !wants(next, v)) {
            next.push_back(v);  // only negotiated cameras can be enabled without renegotiating
        }
    }

    std::vector<std::string> newly_enabled;
    std::string summary;
    for (auto& c : cameras_) {
        const bool was = wants(peer->active, c->name);
        const bool now = wants(next, c->name);
        if (now && !was) {
            c->want.fetch_add(1, std::memory_order_relaxed);
            newly_enabled.push_back(c->name);
        } else if (!now && was) {
            c->want.fetch_sub(1, std::memory_order_relaxed);
        }
        if (now) summary += (summary.empty() ? "" : "+") + c->name;
    }
    peer->active = next;

    // Force an IDR on each newly-enabled camera so the browser's existing (idle) transceiver decodes the
    // resumed stream within a frame instead of waiting for the next periodic keyframe.
    for (const auto& cam : newly_enabled) {
        force_keyframe(cam);
    }

    RCLCPP_INFO(this->get_logger(), "Peer '%s' active streams -> [%s] (no reneg)",
                peer->client_id.empty() ? "(default)" : peer->client_id.c_str(),
                summary.empty() ? "none" : summary.c_str());
}

void WebRTCStreamer::on_negotiation_needed(GstElement* webrtc, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(webrtc), "client_id"));
    auto* genp =
        static_cast<std::shared_ptr<std::atomic<uint64_t>>*>(g_object_get_data(G_OBJECT(webrtc), "mars_gen"));
    if (!genp) {
        return;
    }
    // Offer exactly once per peer: a later renegotiation (e.g. when media starts) must not re-offer and
    // disrupt a live connection.
    if (g_object_get_data(G_OBJECT(webrtc), "mars_offered")) {
        return;
    }
    g_object_set_data(G_OBJECT(webrtc), "mars_offered", GINT_TO_POINTER(1));

    const std::string client_id = cid ? cid : "";
    auto* ctx = new OfferContext{self, GST_ELEMENT(gst_object_ref(webrtc)), *genp, (*genp)->load(), client_id};
    GstPromise* promise = gst_promise_new_with_change_func(on_offer_created, ctx, offer_context_free);
    g_signal_emit_by_name(webrtc, "create-offer", nullptr, promise);
    RCLCPP_INFO(self->get_logger(), "Negotiation needed for '%s'; offering...",
                client_id.empty() ? "(default)" : client_id.c_str());
}

void WebRTCStreamer::destroy_peer(const std::string& client_id) {
    auto it = peers_.find(client_id);
    if (it == peers_.end()) {
        return;
    }
    Peer* p = it->second.get();
    p->generation->fetch_add(1, std::memory_order_relaxed);  // invalidate any in-flight offer
    // Mirror create/update: the want-count tracks ACTIVE cameras, so release exactly what this peer held.
    for (const auto& v : p->active) {
        if (CameraEncoder* c = find_camera(v)) c->want.fetch_sub(1, std::memory_order_relaxed);
    }

    // NULL first: joins the transport's streaming threads, so no probe/callback runs after this point.
    if (p->pipeline) {
        gst_element_set_state(p->pipeline, GST_STATE_NULL);
    }
    for (auto& kv : p->rtp) gst_object_unref(kv.second);
    if (p->webrtc) gst_object_unref(p->webrtc);
    if (p->pipeline) gst_object_unref(p->pipeline);

    RCLCPP_INFO(this->get_logger(), "Released peer '%s'", client_id.empty() ? "(default)" : client_id.c_str());
    peers_.erase(it);
    reconcile_subscriptions();  // last peer wanting a camera may have just left — drop its sub if so
}

// =============================================================================
// Signaling
// =============================================================================

void WebRTCStreamer::on_start(const std_msgs::msg::String::SharedPtr msg) {
    std::string source = "live";
    bool request_audio = false;
    std::string client_id;
    std::vector<std::string> videos;
    bool video_specified = false;
    if (!msg->data.empty()) {
        try {
            auto json = nlohmann::json::parse(msg->data);
            if (json.contains("source")) source = json["source"].get<std::string>();
            if (json.contains("audio")) request_audio = json["audio"].get<bool>();
            if (json.contains("client_id")) client_id = json["client_id"].get<std::string>();
            if (json.contains("video") && json["video"].is_array()) {
                video_specified = true;
                for (const auto& e : json["video"]) {
                    if (!e.is_string()) continue;
                    const std::string s = e.get<std::string>();
                    if (find_camera(s) && !wants(videos, s)) {
                        videos.push_back(s);  // accept any configured camera name
                    }
                }
            }
        } catch (const nlohmann::json::exception&) {
        }
    }
    if (!video_specified) {
        for (auto& cam : cameras_) videos.push_back(cam->name);  // default: all configured cameras
    }

    std::string video_list;
    for (const auto& v : videos) {
        video_list += (video_list.empty() ? "" : "+") + v;
    }
    RCLCPP_INFO(this->get_logger(), "START '%s' (source=%s, video=[%s], audio=%s)",
                client_id.empty() ? "(default)" : client_id.c_str(), source.c_str(), video_list.c_str(),
                request_audio ? "requested" : "off");

    std::lock_guard<std::mutex> lock(peers_mutex_);

    // Source is global across peers; re-point the shared subscriptions if it changed.
    if (source != current_source_) {
        current_source_ = source;
        destroy_subscriptions();    // topics changed; drop the old-source subs...
        reconcile_subscriptions();  // ...and re-subscribe (lazily) whatever current peers still negotiate
        RCLCPP_INFO(this->get_logger(), "Global source switched to %s", source.c_str());
    }

    const bool with_audio = enable_audio_ && request_audio;

    if (videos.empty() && !with_audio) {
        RCLCPP_INFO(this->get_logger(), "START requested no streams; releasing peer");
        destroy_peer(client_id);
        return;
    }

    // Stream switch on an already-set-up independent peer (audio unchanged): flip which cameras are pushed
    // live, with no renegotiation/ICE — so switching is instant instead of a ~connect-latency reconnect.
    if (!client_id.empty()) {
        auto it = peers_.find(client_id);
        if (it != peers_.end() && it->second->with_audio == with_audio) {
            update_peer_active(it->second.get(), videos);
            return;
        }
    }

    // Connect (or audio change, or the legacy default peer). Independent peers negotiate ALL cameras up
    // front so future switches stay reneg-free; the legacy peer negotiates exactly what it asked for.
    std::vector<std::string> all_cams;
    for (auto& cam : cameras_) all_cams.push_back(cam->name);
    const std::vector<std::string> negotiated = client_id.empty() ? videos : all_cams;
    if (!create_peer_transport(client_id, negotiated, videos, with_audio)) {
        if (with_audio) {
            RCLCPP_WARN(this->get_logger(), "Transport failed with audio; retrying video-only");
            if (!create_peer_transport(client_id, negotiated, videos, false)) {
                RCLCPP_ERROR(this->get_logger(), "Failed to start transport for peer");
            }
        } else {
            RCLCPP_ERROR(this->get_logger(), "Failed to start transport for peer");
        }
    }
}

void WebRTCStreamer::publish_offer(const std::string& client_id, const std::string& sdp) {
    auto msg = std_msgs::msg::String();
    if (client_id.empty()) {
        msg.data = sdp;  // legacy: raw SDP
        offer_pub_->publish(msg);
    } else {
        nlohmann::json j;
        j["client_id"] = client_id;
        j["sdp"] = sdp;
        msg.data = j.dump();
        offer_id_pub_->publish(msg);
    }
}

void WebRTCStreamer::on_offer_created(GstPromise* promise, gpointer user_data) {
    auto* ctx = static_cast<OfferContext*>(user_data);  // freed by offer_context_free with the promise
    auto* self = ctx->self;

    gst_promise_wait(promise);
    const GstStructure* reply = gst_promise_get_reply(promise);
    GstWebRTCSessionDescription* offer = nullptr;
    gst_structure_get(reply, "offer", GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &offer, nullptr);
    if (!offer) {
        RCLCPP_ERROR(self->get_logger(), "Failed to create offer");
        gst_promise_unref(promise);
        return;
    }

    // Drop the offer if the peer was torn down / replaced while it was being built (lock-free check).
    if (ctx->gen->load(std::memory_order_relaxed) != ctx->gen_value) {
        RCLCPP_INFO(self->get_logger(), "Discarding stale offer for '%s'",
                    ctx->client_id.empty() ? "(default)" : ctx->client_id.c_str());
        gst_webrtc_session_description_free(offer);
        gst_promise_unref(promise);
        return;
    }

    // Safety net: never publish/apply an offer with no media. Applying the matching (empty) answer to a
    // webrtcbin that still holds transceivers is the _connect_input_stream abort. on-negotiation-needed
    // should prevent this, but a dropped offer just trips the connect timeout — an abort kills the robot.
    if (gst_sdp_message_medias_len(offer->sdp) == 0) {
        RCLCPP_WARN(self->get_logger(), "Dropping empty offer for '%s' (no media sections)",
                    ctx->client_id.empty() ? "(default)" : ctx->client_id.c_str());
        gst_webrtc_session_description_free(offer);
        gst_promise_unref(promise);
        return;
    }

    g_signal_emit_by_name(ctx->webrtc, "set-local-description", offer, nullptr);  // fire-and-forget

    gchar* sdp_text = gst_sdp_message_as_text(offer->sdp);
    std::string sdp_str(sdp_text);
    g_free(sdp_text);
    self->publish_offer(ctx->client_id, sdp_str);
    RCLCPP_INFO(self->get_logger(), "Sent offer for '%s' (%zu bytes)",
                ctx->client_id.empty() ? "(default)" : ctx->client_id.c_str(), sdp_str.size());

    gst_webrtc_session_description_free(offer);
    gst_promise_unref(promise);
}

void WebRTCStreamer::apply_answer(Peer* peer, const std::string& sdp) {
    GstSDPMessage* sdp_msg = nullptr;
    if (gst_sdp_message_new_from_text(sdp.c_str(), &sdp_msg) != GST_SDP_OK) {
        RCLCPP_ERROR(this->get_logger(), "Failed to parse SDP answer");
        return;
    }
    GstWebRTCSessionDescription* answer = gst_webrtc_session_description_new(GST_WEBRTC_SDP_TYPE_ANSWER, sdp_msg);
    // Fire-and-forget: pass a NULL promise (we don't observe the result), same as set-local-description in
    // on_offer_created. Passing a gst_promise_new() here would leak it — it's never waited on or unref'd.
    g_signal_emit_by_name(peer->webrtc, "set-remote-description", answer, nullptr);
    gst_webrtc_session_description_free(answer);
    RCLCPP_INFO(this->get_logger(), "Answer set for '%s'", peer->client_id.empty() ? "(default)" : peer->client_id.c_str());
}

std::string WebRTCStreamer::prepare_ice_candidate(const std::string& candidate) {
    const std::string addr = candidate_address(candidate);
    if (!is_mdns_address(addr)) {
        return candidate;  // already an IP (LAN / tailscale / IPv6) — forward unchanged
    }
    // Resolve the browser's mDNS name ourselves, off the peers_ lock, with a short deadline: the reachable
    // LAN .local resolves in ~30 ms; the unreachable ones never resolve and would otherwise stall libnice
    // ~5 s. Forward the resolved IP (so libnice doesn't resolve again); drop the ones that time out.
    const std::string ip = resolve_ipv4_timeout(addr, 200);
    if (ip.empty()) {
        RCLCPP_INFO(this->get_logger(), "Dropping unresolvable mDNS ICE candidate %s (would stall ICE ~5 s)",
                    addr.c_str());
        return "";
    }
    return replace_first(candidate, addr, ip);
}

void WebRTCStreamer::apply_ice(Peer* peer, const std::string& candidate, int mline) {
    g_signal_emit_by_name(peer->webrtc, "add-ice-candidate", mline, candidate.c_str());
}

void WebRTCStreamer::on_answer(const std_msgs::msg::String::SharedPtr msg) {
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find("");
    if (it == peers_.end()) {
        RCLCPP_WARN(this->get_logger(), "Received bare answer but no default peer active");
        return;
    }
    apply_answer(it->second.get(), msg->data);
}

void WebRTCStreamer::on_answer_id(const std_msgs::msg::String::SharedPtr msg) {
    std::string client_id, sdp;
    try {
        auto json = nlohmann::json::parse(msg->data);
        client_id = json.at("client_id").get<std::string>();
        sdp = json.at("sdp").get<std::string>();
    } catch (const nlohmann::json::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Bad answer_id: %s", e.what());
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it == peers_.end()) {
        RCLCPP_WARN(this->get_logger(), "answer_id for unknown peer '%s'", client_id.c_str());
        return;
    }
    apply_answer(it->second.get(), sdp);
}

void WebRTCStreamer::on_ice_in(const std_msgs::msg::String::SharedPtr msg) {
    std::string candidate;
    int mline = 0;
    try {
        auto json = nlohmann::json::parse(msg->data);
        candidate = json["candidate"].get<std::string>();
        mline = json["sdpMLineIndex"].get<int>();
    } catch (const nlohmann::json::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Failed to parse ICE candidate: %s", e.what());
        return;
    }
    const std::string prepared = prepare_ice_candidate(candidate);  // resolve mDNS BEFORE taking the lock
    if (prepared.empty()) {
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find("");
    if (it == peers_.end()) {
        return;
    }
    apply_ice(it->second.get(), prepared, mline);
}

void WebRTCStreamer::on_ice_in_id(const std_msgs::msg::String::SharedPtr msg) {
    std::string client_id, candidate;
    int mline = 0;
    try {
        auto json = nlohmann::json::parse(msg->data);
        client_id = json.at("client_id").get<std::string>();
        candidate = json.at("candidate").get<std::string>();
        mline = json.at("sdpMLineIndex").get<int>();
    } catch (const nlohmann::json::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Bad ice_in_id: %s", e.what());
        return;
    }
    const std::string prepared = prepare_ice_candidate(candidate);  // resolve mDNS BEFORE taking the lock
    if (prepared.empty()) {
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it == peers_.end()) {
        return;
    }
    apply_ice(it->second.get(), prepared, mline);
}

void WebRTCStreamer::on_ice_candidate(GstElement* webrtc, guint mline, gchar* candidate, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    // Send all of our own candidates (IPv4 LAN, tailscale, IPv6); the browser picks what reaches it.
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(webrtc), "client_id"));
    std::string client_id = cid ? cid : "";

    nlohmann::json json;
    json["candidate"] = std::string(candidate);
    json["sdpMLineIndex"] = mline;

    auto msg = std_msgs::msg::String();
    if (client_id.empty()) {
        msg.data = json.dump();
        self->ice_out_pub_->publish(msg);
    } else {
        json["client_id"] = client_id;
        msg.data = json.dump();
        self->ice_out_id_pub_->publish(msg);
    }
}

void WebRTCStreamer::on_connection_state_changed(GstElement* webrtc, GParamSpec*, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    GstWebRTCPeerConnectionState state;
    g_object_get(webrtc, "connection-state", &state, nullptr);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(webrtc), "client_id"));

    // On connect, force a keyframe on both encoders so this peer (which may be joining a stream that's
    // already running for others) gets a decodable IDR immediately. Teardown is handled by the health
    // poll on the executor thread (set-state from here would deadlock the pipeline).
    if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
        self->force_keyframe("main");
        self->force_keyframe("arm");
    }
    RCLCPP_INFO(self->get_logger(), "Peer '%s' connection state: %s", (cid && *cid) ? cid : "(default)",
                conn_state_name(state));
}

// =============================================================================
// RTCP-inactivity watchdog + health poll
// =============================================================================

GstPadProbeReturn WebRTCStreamer::on_rtcp_buffer(GstPad*, GstPadProbeInfo*, gpointer user_data) {
    auto* peer = static_cast<Peer*>(user_data);
    peer->last_rtcp_ns.store(std::chrono::steady_clock::now().time_since_epoch().count(), std::memory_order_relaxed);
    return GST_PAD_PROBE_OK;
}

GstPadProbeReturn WebRTCStreamer::on_audio_buffer(GstPad*, GstPadProbeInfo*, gpointer user_data) {
    static_cast<Peer*>(user_data)->audio_pkts.fetch_add(1, std::memory_order_relaxed);
    return GST_PAD_PROBE_OK;
}

bool WebRTCStreamer::install_rtcp_probe_for(Peer* peer) {
    GstElement* rtpbin = gst_bin_get_by_name(GST_BIN(peer->webrtc), "rtpbin");
    if (!rtpbin) {
        return false;
    }
    bool installed = false;
    GstIterator* it = gst_element_iterate_sink_pads(rtpbin);
    GValue item = G_VALUE_INIT;
    bool done = false;
    while (!done) {
        switch (gst_iterator_next(it, &item)) {
            case GST_ITERATOR_OK: {
                GstPad* pad = GST_PAD(g_value_get_object(&item));
                gchar* name = gst_pad_get_name(pad);
                if (name && g_str_has_prefix(name, "recv_rtcp_sink")) {
                    gst_pad_add_probe(pad,
                                      static_cast<GstPadProbeType>(GST_PAD_PROBE_TYPE_BUFFER |
                                                                   GST_PAD_PROBE_TYPE_BUFFER_LIST),
                                      on_rtcp_buffer, peer, nullptr);
                    installed = true;
                }
                g_free(name);
                g_value_reset(&item);
                break;
            }
            case GST_ITERATOR_RESYNC:
                gst_iterator_resync(it);
                break;
            case GST_ITERATOR_ERROR:
            case GST_ITERATOR_DONE:
                done = true;
                break;
        }
    }
    g_value_unset(&item);
    gst_iterator_free(it);
    gst_object_unref(rtpbin);
    return installed;
}

void WebRTCStreamer::poll_pipeline_health() {
    std::lock_guard<std::mutex> lock(peers_mutex_);
    if (peers_.empty()) {
        return;
    }
    const int64_t now_ns = std::chrono::steady_clock::now().time_since_epoch().count();
    std::vector<std::string> dead;

    for (auto& kv : peers_) {
        Peer* p = kv.second.get();
        // Drain this peer's bus so runtime errors are logged.
        GstBus* bus = gst_element_get_bus(p->pipeline);
        while (GstMessage* m = gst_bus_pop(bus)) {
            if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR) {
                GError* err = nullptr;
                gst_message_parse_error(m, &err, nullptr);
                RCLCPP_ERROR(this->get_logger(), "GStreamer error from %s: %s", GST_OBJECT_NAME(m->src),
                             err ? err->message : "unknown");
                g_clear_error(&err);
            }
            gst_message_unref(m);
        }
        gst_object_unref(bus);

        const GstWebRTCPeerConnectionState state = peer_connection_state(p->webrtc);
        const bool closed = state == GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED;
        const bool down = state == GST_WEBRTC_PEER_CONNECTION_STATE_FAILED ||
                          state == GST_WEBRTC_PEER_CONNECTION_STATE_DISCONNECTED;
        p->terminal_polls = down ? p->terminal_polls + 1 : 0;
        if (closed || p->terminal_polls >= kTeardownGracePolls) {
            RCLCPP_INFO(this->get_logger(), "Peer '%s' %s; releasing", kv.first.empty() ? "(default)" : kv.first.c_str(),
                        closed ? "closed" : "down past grace window");
            dead.push_back(kv.first);
            continue;
        }

        // Release a peer that never finished connecting (failed ICE / abandoned handshake) so it can't
        // leak its transport and keep the encoder pinned on.
        if (!p->ever_connected && state != GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
            if ((now_ns - p->created_ns) / 1e9 > kConnectTimeoutS) {
                RCLCPP_INFO(this->get_logger(), "Peer '%s' never connected within %.0f s; releasing",
                            kv.first.empty() ? "(default)" : kv.first.c_str(), kConnectTimeoutS);
                dead.push_back(kv.first);
                continue;
            }
        }

        if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
            p->ever_connected = true;
            if (!p->rtcp_probe_installed) {
                p->rtcp_probe_installed = install_rtcp_probe_for(p);
                if (p->rtcp_probe_installed) {
                    p->last_rtcp_ns.store(now_ns, std::memory_order_relaxed);  // arm from connect time
                }
            }
            const int64_t last = p->last_rtcp_ns.load(std::memory_order_relaxed);
            if (last != 0 && p->rtcp_probe_installed) {
                const double idle_s = (now_ns - last) / 1e9;
                if (idle_s > rtcp_inactivity_timeout_s_) {
                    RCLCPP_INFO(this->get_logger(), "Peer '%s' RTCP idle %.1f s (> %.1f s); releasing",
                                kv.first.empty() ? "(default)" : kv.first.c_str(), idle_s, rtcp_inactivity_timeout_s_);
                    dead.push_back(kv.first);
                }
            }
        }
    }

    for (const auto& id : dead) {
        destroy_peer(id);
    }
}

// =============================================================================
// Status
// =============================================================================

void WebRTCStreamer::publish_status() {
    const auto now = std::chrono::steady_clock::now();
    double dt = std::chrono::duration<double>(now - prev_status_time_).count();
    if (dt <= 1e-3) dt = 1e-3;
    prev_status_time_ = now;

    const bool replay = current_source_ == "replay";
    // Sample each camera's node-wide encode fps (each camera is encoded once; shared by all its viewers).
    std::map<std::string, std::pair<double, std::string>> cam_info;  // name -> {fps, source topic}
    for (auto& cam : cameras_) {
        const uint64_t f = cam->frames.load(std::memory_order_relaxed);
        cam_info[cam->name] = {(f - cam->prev_frames) / dt, replay ? cam->replay_topic : cam->live_topic};
        cam->prev_frames = f;
    }

    nlohmann::json clients = nlohmann::json::array();
    {
        std::lock_guard<std::mutex> lock(peers_mutex_);
        for (auto& kv : peers_) {
            Peer* p = kv.second.get();
            const std::string conn = conn_state_name(peer_connection_state(p->webrtc));
            const int64_t last = p->last_rtcp_ns.load(std::memory_order_relaxed);
            double rtcp_age = -1.0;
            if (last != 0) {
                rtcp_age = (now.time_since_epoch().count() - last) / 1e9;
            }
            // fps is a node-wide per-camera rate (the camera is encoded once); reported against each
            // peer that subscribes to it so the dashboard shows whether that stream is live.
            nlohmann::json streams = nlohmann::json::array();
            for (const auto& v : p->active) {  // report streams actually being sent, not merely negotiated
                auto info = cam_info.find(v);
                if (info == cam_info.end()) continue;
                nlohmann::json s;
                s["name"] = v;
                s["topic"] = info->second.second;
                s["fps"] = round1(info->second.first);
                s["encoding"] = info->second.first > 0.5;  // frames entering the encoder => being rendered
                streams.push_back(s);
            }
            if (p->with_audio) {
                nlohmann::json s;
                s["name"] = "audio";
                s["encoding"] = p->audio_pkts.load(std::memory_order_relaxed) > 0;
                streams.push_back(s);
            }
            nlohmann::json c;
            c["client_id"] = p->client_id;
            c["source"] = current_source_;
            c["audio"] = p->with_audio;
            c["connection_state"] = conn;
            if (rtcp_age >= 0.0) {
                c["rtcp_age_s"] = std::round(rtcp_age * 100.0) / 100.0;
            }
            c["streams"] = streams;
            clients.push_back(c);
        }
    }

    nlohmann::json root;
    root["count"] = clients.size();
    root["clients"] = clients;

    std_msgs::msg::String msg;
    msg.data = root.dump();
    active_streams_pub_->publish(msg);
}

}  // namespace mars_cam

RCLCPP_COMPONENTS_REGISTER_NODE(mars_cam::WebRTCStreamer)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char* argv[]) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<mars_cam::WebRTCStreamer>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
#endif
