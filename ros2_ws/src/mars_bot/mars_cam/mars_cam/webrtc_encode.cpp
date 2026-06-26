#include "mars_cam/webrtc_streamer.hpp"
#include "mars_cam/webrtc_internal.hpp"

#include <gst/app/app.h>
#include <gst/rtp/rtp.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <memory>
#include <sstream>

namespace mars_cam {

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
// Shared audio (mic): encoded once, fanned out, gated NULL/PLAYING for mic privacy
// =============================================================================

bool WebRTCStreamer::build_audio_pipeline() {
    // Validate the mic element + device (a plain element name / device string — these go into a parsed
    // pipeline description, so reject anything that could inject extra elements).
    const bool valid_element = !audio_source_element_.empty() &&
                               std::all_of(audio_source_element_.begin(), audio_source_element_.end(),
                                           [](unsigned char c) { return std::isalnum(c) || c == '-' || c == '_'; });
    if (!valid_element) {
        RCLCPP_ERROR(this->get_logger(), "audio_source_element '%s' is not a plain element name",
                     audio_source_element_.c_str());
        return false;
    }
    std::string src = audio_source_element_;
    if (!audio_capture_device_.empty()) {
        const bool valid_device =
            std::all_of(audio_capture_device_.begin(), audio_capture_device_.end(), [](unsigned char c) {
                return std::isalnum(c) || c == ':' || c == ',' || c == '.' || c == '=' || c == '-' || c == '_' ||
                       c == '/';
            });
        if (!valid_device) {
            RCLCPP_ERROR(this->get_logger(), "audio_capture_device '%s' has unexpected chars", audio_capture_device_.c_str());
            return false;
        }
        src += " device=\"" + audio_capture_device_ + "\"";
    }
    // mic -> opus -> rtp -> appsink (the fan-out tap). Encoded once for all peers; matches the RTP caps
    // each peer's transport audio appsrc declares (OPUS/48000/pt98).
    std::string desc = src +
                       " do-timestamp=true ! "
                       "queue leaky=downstream max-size-buffers=10 max-size-time=0 max-size-bytes=0 ! "
                       "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! "
                       "opusenc bitrate=24000 audio-type=voice ! "
                       "rtpopuspay name=pay_audio pt=98 ! "
                       "appsink name=sink_audio emit-signals=true sync=false max-buffers=4 drop=true ";
    GError* error = nullptr;
    audio_pipeline_ = gst_parse_launch(desc.c_str(), &error);
    if (error) {
        RCLCPP_ERROR(this->get_logger(), "Failed to create audio pipeline: %s", error->message);
        g_error_free(error);
        if (audio_pipeline_) {
            gst_object_unref(audio_pipeline_);
            audio_pipeline_ = nullptr;
        }
        return false;
    }
    audio_sink_ = gst_bin_get_by_name(GST_BIN(audio_pipeline_), "sink_audio");
    if (!audio_sink_) {
        RCLCPP_ERROR(this->get_logger(), "Audio pipeline missing appsink");
        return false;
    }
    g_signal_connect(audio_sink_, "new-sample", G_CALLBACK(on_audio_sample), this);
    // Stays NULL (mic closed) until a peer activates audio — reconcile_audio() opens it.
    RCLCPP_INFO(this->get_logger(), "Shared audio pipeline built (mic '%s', closed until a peer wants audio)",
                src.c_str());
    return true;
}

void WebRTCStreamer::reconcile_audio() {
    // Mic-privacy gate: the mic pipeline is PLAYING only while some peer has audio ACTIVE, NULL otherwise
    // (the device is genuinely closed). Opening (NULL->PLAYING) starts threads and never blocks, so it is
    // safe to call under peers_mutex_; CLOSING (->NULL) JOINS the fan-out streaming thread, which also
    // takes peers_mutex_ — so the close path must only run with the lock NOT held (it's driven by the
    // health poll). reconcile_audio() is therefore: callable under the lock when want_audio_>0 (opens),
    // and from poll_pipeline_health (outside the lock) for the general case (also closes).
    if (!audio_pipeline_) {
        return;
    }
    const bool want = want_audio_.load(std::memory_order_relaxed) > 0;
    if (want == audio_playing_) {
        return;
    }
    if (want) {
        if (gst_element_set_state(audio_pipeline_, GST_STATE_PLAYING) != GST_STATE_CHANGE_FAILURE) {
            audio_playing_ = true;
            RCLCPP_INFO(this->get_logger(), "Mic opened (a peer activated audio)");
        } else {
            RCLCPP_WARN(this->get_logger(), "Mic failed to open");
        }
    } else {
        gst_element_set_state(audio_pipeline_, GST_STATE_NULL);  // closes the device; joins the fan-out thread
        audio_playing_ = false;
        RCLCPP_INFO(this->get_logger(), "Mic closed (no peer wants audio)");
    }
}

GstFlowReturn WebRTCStreamer::on_audio_sample(GstElement* appsink, gpointer user_data) {
    static_cast<WebRTCStreamer*>(user_data)->fan_out_audio(appsink);
    return GST_FLOW_OK;
}

void WebRTCStreamer::fan_out_audio(GstElement* appsink) {
    GstSample* sample = gst_app_sink_pull_sample(GST_APP_SINK(appsink));
    if (!sample) {
        return;
    }
    GstBuffer* buffer = gst_sample_get_buffer(sample);
    if (buffer) {
        audio_frames_.fetch_add(1, std::memory_order_relaxed);
        // Same brief-lock + ref pattern as the video fan-out (see fan_out_sample).
        std::vector<GstElement*> targets;
        {
            std::lock_guard<std::mutex> lock(peers_mutex_);
            for (auto& kv : peers_) {
                auto it = kv.second->rtp.find("audio");
                if (it != kv.second->rtp.end() && it->second && kv.second->audio_active) {
                    targets.push_back(GST_ELEMENT(gst_object_ref(it->second)));
                }
            }
        }
        for (GstElement* src : targets) {
            GstBuffer* out = gst_buffer_copy(buffer);
            GST_BUFFER_PTS(out) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DTS(out) = GST_CLOCK_TIME_NONE;
            GstFlowReturn ret;
            g_signal_emit_by_name(src, "push-buffer", out, &ret);
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


}  // namespace mars_cam
