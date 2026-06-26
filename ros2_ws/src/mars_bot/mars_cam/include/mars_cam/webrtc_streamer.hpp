#ifndef MARS_CAM__WEBRTC_STREAMER_HPP_
#define MARS_CAM__WEBRTC_STREAMER_HPP_

#define GST_USE_UNSTABLE_API

#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <std_msgs/msg/string.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>

#include <gst/gst.h>
#include <gst/webrtc/webrtc.h>
#include <gst/sdp/sdp.h>

#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>

#include <string>
#include <vector>
#include <memory>
#include <map>
#include <mutex>
#include <chrono>
#include <atomic>

namespace mars_cam {

// One connected WebRTC peer. The cameras are encoded ONCE in a persistent pipeline; each peer owns a
// lightweight *transport* pipeline (appsrc(RTP) -> webrtcbin) that the shared encoders fan their RTP
// out to. Creating/destroying a peer never touches the encoders, so memory stays flat and a dead peer
// can't take the cameras down with it.
struct Peer {
    std::string client_id;            // "" = the legacy/default peer (bare signaling topics)
    GstElement* pipeline = nullptr;   // transport pipeline (owns webrtcbin + rtp appsrcs)
    GstElement* webrtc = nullptr;     // ref'd from pipeline
    GstElement* rtp_main = nullptr;   // ref'd appsrc, null if main not negotiated
    GstElement* rtp_arm = nullptr;    // ref'd appsrc, null if arm not negotiated
    std::vector<std::string> videos;  // NEGOTIATED video streams (transceivers), in m-line order
    std::vector<std::string> active;  // currently PUSHED streams (subset of videos); toggled live on a
                                      // stream switch without renegotiating, so switches are instant
    bool with_audio = false;

    // Stale-offer guard: shared with the async create-offer callback so it can tell its peer was torn
    // down / replaced underneath it (lock-free, and survives the Peer being freed). Bumped on destroy.
    std::shared_ptr<std::atomic<uint64_t>> generation = std::make_shared<std::atomic<uint64_t>>(0);

    // Steady-clock ns when the transport was created; a peer that never reaches CONNECTED within the
    // connect timeout (failed ICE, abandoned handshake) is released so it can't leak its transport.
    int64_t created_ns = 0;
    bool ever_connected = false;

    // Per-peer RTCP-inactivity watchdog (a vanished peer leaves webrtcbin stuck in CONNECTED).
    std::atomic<int64_t> last_rtcp_ns{0};  // steady-clock ns of last RTCP; 0 = disarmed
    bool rtcp_probe_installed = false;
    int terminal_polls = 0;  // consecutive FAILED/DISCONNECTED health polls
    bool audio_probe_installed = false;
    std::atomic<uint64_t> audio_pkts{0};
};

class WebRTCStreamer : public rclcpp::Node {
   public:
    explicit WebRTCStreamer(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());
    ~WebRTCStreamer();

   private:
    // ---- Signaling (ROS). Bare topics = legacy single peer; *_id topics carry {client_id,...} ----
    void on_start(const std_msgs::msg::String::SharedPtr msg);
    void on_answer(const std_msgs::msg::String::SharedPtr msg);      // bare, default peer
    void on_answer_id(const std_msgs::msg::String::SharedPtr msg);   // {client_id, sdp}
    void on_ice_in(const std_msgs::msg::String::SharedPtr msg);      // bare, default peer
    void on_ice_in_id(const std_msgs::msg::String::SharedPtr msg);   // {client_id, candidate, ...}
    void apply_answer(Peer* peer, const std::string& sdp);          // caller holds peers_mutex_
    // Resolve an incoming candidate's mDNS <uuid>.local to its IP (short deadline) so libnice never stalls
    // ~5 s on the browser's unreachable names; returns the IP-rewritten candidate, or "" to drop it. Call
    // WITHOUT peers_mutex_ (it can block briefly). apply_ice adds the prepared candidate (caller holds mutex).
    std::string prepare_ice_candidate(const std::string& candidate);
    void apply_ice(Peer* peer, const std::string& candidate, int mline);  // caller holds peers_mutex_

    // ---- Camera frames -> persistent encoders ----
    void on_image_main_raw(const sensor_msgs::msg::Image::SharedPtr msg);
    void on_image_arm_raw(const sensor_msgs::msg::Image::SharedPtr msg);
    void on_image_main_compressed(const sensor_msgs::msg::CompressedImage::SharedPtr msg);
    void on_image_arm_compressed(const sensor_msgs::msg::CompressedImage::SharedPtr msg);

    // ---- GStreamer callbacks (static for the C callback interface) ----
    static void on_ice_candidate(GstElement* webrtc, guint mline, gchar* candidate, gpointer user_data);
    static void on_connection_state_changed(GstElement* webrtc, GParamSpec* pspec, gpointer user_data);
    static void on_negotiation_needed(GstElement* webrtc, gpointer user_data);
    static void on_offer_created(GstPromise* promise, gpointer user_data);
    // appsink new-sample: pull the encoded RTP buffer and fan it out to every peer wanting this camera.
    static GstFlowReturn on_sample_main(GstElement* appsink, gpointer user_data);
    static GstFlowReturn on_sample_arm(GstElement* appsink, gpointer user_data);
    void fan_out_sample(GstElement* appsink, const std::string& cam);

    // Per-peer RTCP-liveness probe on the peer's rtpbin recv_rtcp_sink pad (ticks per RTCP packet).
    static GstPadProbeReturn on_rtcp_buffer(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);
    static GstPadProbeReturn on_audio_buffer(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);
    bool install_rtcp_probe_for(Peer* peer);  // attach once the peer's rtcp pad exists

    // ---- Persistent encode pipeline (built once in the constructor) ----
    std::string video_encode_branch(const std::string& name, int pt, int fps) const;
    bool build_encode_pipeline();
    void attach_playout_delay_extension(const std::string& cam);  // adds the ext to one payloader
    cv::Mat process_raw_image(const sensor_msgs::msg::Image::SharedPtr& msg, int w, int h);
    cv::Mat process_compressed_image(const sensor_msgs::msg::CompressedImage::SharedPtr& msg, int w, int h);
    void push_frame(GstElement* appsrc, const cv::Mat& frame, GstBufferPool* pool, std::atomic<uint64_t>& counter);
    GstBufferPool* create_frame_pool(int width, int height, int channels);
    void force_keyframe(const std::string& cam);  // request an IDR so a fresh/resumed peer can decode

    // ---- Per-peer transport ----
    // create_peer_transport builds the transport pipeline, wires signals, and kicks off create-offer.
    // Caller holds peers_mutex_. Returns the inserted Peer* (or nullptr on failure).
    Peer* create_peer_transport(const std::string& client_id, const std::vector<std::string>& negotiated,
                                const std::vector<std::string>& active, bool with_audio);
    // Toggle which negotiated cameras a connected peer is being sent, with NO renegotiation: adjusts the
    // per-camera want-count + push gate and forces a keyframe on newly-enabled cameras. Caller holds mutex.
    void update_peer_active(Peer* peer, const std::vector<std::string>& active);
    void destroy_peer(const std::string& client_id);  // caller holds peers_mutex_
    std::string build_transport_description(const std::vector<std::string>& videos, bool& with_audio) const;
    void publish_offer(const std::string& client_id, const std::string& sdp);

    // ---- Camera subscriptions (lazy: a camera is subscribed only while some connected peer negotiates
    // it, and dropped when no peer does — so an idle node with no peers receives no camera frames at all).
    void reconcile_subscriptions();  // subscribe/unsubscribe each camera to match what peers negotiate
    void set_camera_subscribed(const std::string& cam, bool subscribed);
    void destroy_subscriptions();

    // ---- Health / status (executor thread) ----
    void poll_pipeline_health();  // per-peer teardown of dead transports
    void publish_status();        // /webrtc/active_streams snapshot at 0.5 Hz

    // Publishers
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr offer_pub_;       // bare, raw SDP
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr offer_id_pub_;    // {client_id, sdp}
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ice_out_pub_;     // bare
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ice_out_id_pub_;  // {client_id, ...}
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr active_streams_pub_;

    // Subscribers
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr start_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr answer_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr answer_id_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr ice_in_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr ice_in_id_sub_;
    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr image_sub_main_raw_;
    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr image_sub_arm_raw_;
    rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_main_compressed_;
    rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_arm_compressed_;

    // ---- Persistent encode pipeline elements (built once, never torn down until shutdown) ----
    GstElement* encode_pipeline_ = nullptr;
    GstElement* appsrc_main_ = nullptr;
    GstElement* appsrc_arm_ = nullptr;
    GstElement* sink_main_ = nullptr;  // appsink endpoints fanned out to peers
    GstElement* sink_arm_ = nullptr;
    GstBufferPool* pool_main_ = nullptr;
    GstBufferPool* pool_arm_ = nullptr;

    // Push-gating: number of connected peers requesting each camera. The image callback skips all
    // convert/encode/push work when the count is 0, so the encoders stay allocated but burn no CPU at
    // idle. Updated under peers_mutex_ on peer add/remove; read lock-free in the camera callbacks.
    std::atomic<int> want_main_{0};
    std::atomic<int> want_arm_{0};

    // ---- Peers ----
    std::map<std::string, std::unique_ptr<Peer>> peers_;
    std::mutex peers_mutex_;

    // Source mode + topics (global across peers; last START wins, re-points the shared subscriptions).
    std::string current_source_ = "live";
    rclcpp::QoS camera_qos_;
    std::string live_main_topic_, live_arm_topic_, replay_main_topic_, replay_arm_topic_;

    // Timers
    rclcpp::TimerBase::SharedPtr health_timer_;
    rclcpp::TimerBase::SharedPtr status_timer_;

    // RTCP-inactivity watchdog timeout (seconds without RTCP before a peer's transport is released).
    double rtcp_inactivity_timeout_s_ = 5.0;
    rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr rtcp_timeout_cb_;

    // Per-stream encode frame counters (sampled by publish_status for per-stream fps).
    std::atomic<uint64_t> main_frames_{0};
    std::atomic<uint64_t> arm_frames_{0};
    uint64_t prev_main_frames_ = 0, prev_arm_frames_ = 0;
    std::chrono::steady_clock::time_point prev_status_time_;

    // Config
    bool use_compressed_images_ = false;
    bool enable_audio_ = true;
    std::string audio_source_element_ = "alsasrc";
    std::string audio_capture_device_;
    guint playout_min_delay_ms_ = 0;
    guint playout_max_delay_ms_ = 40;
};

}  // namespace mars_cam

#endif  // MARS_CAM__WEBRTC_STREAMER_HPP_
