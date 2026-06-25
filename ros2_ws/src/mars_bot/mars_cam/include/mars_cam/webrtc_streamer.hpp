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
#include <mutex>
#include <chrono>
#include <atomic>

namespace mars_cam {

class WebRTCStreamer : public rclcpp::Node {
   public:
    explicit WebRTCStreamer(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());
    ~WebRTCStreamer();

   private:
    // ROS2 callbacks
    void on_start(const std_msgs::msg::String::SharedPtr msg);
    void on_answer(const std_msgs::msg::String::SharedPtr msg);
    void on_ice_in(const std_msgs::msg::String::SharedPtr msg);
    void on_image_main_raw(const sensor_msgs::msg::Image::SharedPtr msg);
    void on_image_arm_raw(const sensor_msgs::msg::Image::SharedPtr msg);
    void on_image_main_compressed(const sensor_msgs::msg::CompressedImage::SharedPtr msg);
    void on_image_arm_compressed(const sensor_msgs::msg::CompressedImage::SharedPtr msg);

    // GStreamer callbacks (static for C callback interface)
    static void on_ice_candidate(GstElement* webrtc, guint mline, gchar* candidate, gpointer user_data);
    static void on_connection_state_changed(GstElement* webrtc, GParamSpec* pspec, gpointer user_data);
    static void on_ice_gathering_state_changed(GstElement* webrtc, GParamSpec* pspec, gpointer user_data);
    static void on_offer_created(GstPromise* promise, gpointer user_data);

    // Buffer probe on rtpbin's recv_rtcp_sink pad: fires for every decrypted RTCP packet the peer
    // sends (receiver reports, transport-cc feedback), so it ticks continuously while the peer is
    // alive. Used as the liveness signal for the inactivity watchdog.
    static GstPadProbeReturn on_rtcp_buffer(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);
    bool install_rtcp_probe_locked();  // attach the probe once the session's rtcp pad exists
    void note_rtcp_activity();          // stamps last_rtcp_activity_ns_ with the steady clock

    // Builds the /webrtc/active_streams JSON from the live pipeline and publishes it (0.5 Hz timer).
    void publish_status();

    // Helper methods. `videos` is the ordered set of requested video streams ("main"/"arm"); only
    // those are subscribed/encoded/offered, so a client can ask for a subset (selective encode).
    void create_subscriptions(const std::string& source, const std::vector<std::string>& videos);
    void destroy_subscriptions();
    void cleanup_pipeline();

    // Runs on the executor thread (never a GStreamer thread): drains the bus so runtime
    // errors are logged, and tears the pipeline down once the peer connection is gone.
    void poll_pipeline_health();
    void drain_bus_locked();  // caller must hold pipeline_mutex_

    // Builds the pipeline for the requested video streams (in order: sink_0, sink_1, ...) plus an
    // optional Opus mic branch on the next sink. with_audio is cleared to false if the audio params
    // are unsafe, so callers log the real state.
    std::string build_pipeline_description(const std::vector<std::string>& videos, bool& with_audio) const;

    // *_locked: caller must hold pipeline_mutex_. start_ returns false (and tears down) on failure.
    // with_audio is updated to reflect whether audio actually made it into the pipeline.
    bool start_pipeline_locked(const std::vector<std::string>& videos, bool& with_audio);
    void teardown_pipeline_locked();

    // Adds the playout-delay RTP header extension to each present video track (caller holds the mutex).
    void attach_playout_delay_extension(const std::vector<std::string>& videos);
    cv::Mat process_raw_image(const sensor_msgs::msg::Image::SharedPtr& msg, int target_width, int target_height);
    cv::Mat process_compressed_image(const sensor_msgs::msg::CompressedImage::SharedPtr& msg, int target_width,
                                     int target_height);
    void push_frame(GstElement* appsrc, const cv::Mat& frame, GstBufferPool* pool);
    GstBufferPool* create_frame_pool(int width, int height, int channels);

    // Publishers
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr offer_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ice_out_pub_;
    // /webrtc/active_streams: a JSON snapshot of the connected client(s) and each video/audio
    // stream's live encode status, published at 0.5 Hz for the debug dashboard.
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr active_streams_pub_;

    // Subscribers
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr answer_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr ice_in_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr start_sub_;
    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr image_sub_main_raw_;
    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr image_sub_arm_raw_;
    rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_main_compressed_;
    rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_arm_compressed_;

    // GStreamer elements (initialized first in constructor)
    GstElement* pipeline_;
    GstElement* webrtc_;
    GstElement* appsrc_main_;
    GstElement* appsrc_arm_;

    // Buffer pools for zero-alloc frame pushing
    GstBufferPool* pool_main_;
    GstBufferPool* pool_arm_;

    // Source mode (initialized early)
    std::string current_source_;

    // QoS profile for cameras
    rclcpp::QoS camera_qos_;

    // Topic configuration
    std::string live_main_topic_;
    std::string live_arm_topic_;
    std::string replay_main_topic_;
    std::string replay_arm_topic_;

    // Thread safety
    std::mutex pipeline_mutex_;

    // Periodic bus drain + disconnect teardown
    rclcpp::TimerBase::SharedPtr health_timer_;
    int terminal_polls_ = 0;  // consecutive FAILED/DISCONNECTED polls (grace window before teardown)

    // RTCP-inactivity watchdog. webrtcbin never reports FAILED/DISCONNECTED when a peer vanishes
    // ungracefully (closed tab, killed process), so a "connected" pipeline — and its camera
    // subscriptions, encoders and mic — would otherwise leak until the next START. A live receiver
    // streams RTCP continuously (RR ~1/s, transport-cc ~10-20/s); if none arrives for longer than
    // this timeout we treat the peer as gone and tear the pipeline down. Tunable via ros param.
    double rtcp_inactivity_timeout_s_ = 5.0;
    // Lets the timeout be retuned at runtime via `ros2 param set` (read on the executor thread, same
    // as poll_pipeline_health, so no extra locking).
    rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr rtcp_timeout_cb_;
    // Steady-clock ns of the last RTCP from the peer, or 0 when the watchdog is disarmed (no peer
    // connected yet). Written from a GStreamer thread, read on the executor thread.
    std::atomic<int64_t> last_rtcp_activity_ns_{0};
    bool rtcp_probe_installed_ = false;  // recv-RTCP buffer probe attached for this connection

    // Bumped on every teardown so an in-flight create-offer callback (which runs on a GStreamer
    // thread, after on_start has released the lock) can tell its pipeline was torn down underneath
    // it and drop the stale offer instead of touching a freed webrtcbin. This is the live->replay->
    // live crash fix: rapid STARTs used to race teardown against the offer callback.
    std::atomic<uint64_t> pipeline_generation_{0};

    // Per-connection client info (from START) + live stream-status monitoring for active_streams.
    std::string client_id_;
    std::string active_source_;  // "live" / "replay" actually subscribed
    std::vector<std::string> active_video_;  // video streams actually built/encoded, in m-line order
    bool with_audio_ = false;
    // Frames pushed into each encoder appsrc / audio RTP packets seen — sampled by the status timer
    // to report per-stream encode fps (0 fps => source silent => "not being rendered").
    std::atomic<uint64_t> main_frames_{0};
    std::atomic<uint64_t> arm_frames_{0};
    std::atomic<uint64_t> audio_pkts_{0};
    static GstPadProbeReturn on_audio_buffer(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);
    bool audio_probe_installed_ = false;
    // Previous sample for fps deltas in publish_status().
    uint64_t prev_main_frames_ = 0, prev_arm_frames_ = 0, prev_audio_pkts_ = 0;
    std::chrono::steady_clock::time_point prev_status_time_;
    rclcpp::TimerBase::SharedPtr status_timer_;

    // Use compressed images (for sim/rosbridge)
    bool use_compressed_images_;

    // Robot microphone -> teleoperator audio (one-way).
    bool enable_audio_;
    std::string audio_source_element_;
    std::string audio_capture_device_;

    // Receiver de-jitter buffer bounds (ms) signalled via the playout-delay header extension.
    guint playout_min_delay_ms_ = 0;
    guint playout_max_delay_ms_ = 40;
};

}  // namespace mars_cam

#endif  // MARS_CAM__WEBRTC_STREAMER_HPP_
