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

namespace {
struct SdpMediaCounts {
    guint video = 0;
    guint audio = 0;
    guint application = 0;
};

SdpMediaCounts count_sdp_media(const GstSDPMessage* sdp) {
    SdpMediaCounts counts;
    if (!sdp) {
        return counts;
    }
    const guint media_count = gst_sdp_message_medias_len(sdp);
    for (guint i = 0; i < media_count; ++i) {
        const GstSDPMedia* media = gst_sdp_message_get_media(sdp, i);
        const char* kind = media ? gst_sdp_media_get_media(media) : nullptr;
        if (!kind) {
            continue;
        }
        if (g_strcmp0(kind, "video") == 0) {
            counts.video += 1;
        } else if (g_strcmp0(kind, "audio") == 0) {
            counts.audio += 1;
        } else if (g_strcmp0(kind, "application") == 0) {
            counts.application += 1;
        }
    }
    return counts;
}
}  // namespace

// =============================================================================
// Signaling
// =============================================================================

void WebRTCStreamer::on_start(const std_msgs::msg::String::SharedPtr msg) {
    std::string source = "live";
    bool request_audio = false;
    std::string client_id;
    std::vector<std::string> videos;
    bool video_specified = false;
    bool renegotiate = false;
    if (!msg->data.empty()) {
        try {
            auto json = nlohmann::json::parse(msg->data);
            if (json.contains("source")) source = json["source"].get<std::string>();
            if (json.contains("audio")) request_audio = json["audio"].get<bool>();
            if (json.contains("client_id")) client_id = json["client_id"].get<std::string>();
            if (json.contains("renegotiate")) renegotiate = json["renegotiate"].get<bool>();
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
    RCLCPP_INFO(this->get_logger(), "START '%s' (source=%s, video=[%s], audio=%s, renegotiate=%s)",
                client_id.empty() ? "(default)" : client_id.c_str(), source.c_str(), video_list.c_str(),
                request_audio ? "requested" : "off", renegotiate ? "true" : "false");

    std::lock_guard<std::mutex> lock(peers_mutex_);

    // Source is global across peers; re-point the shared subscriptions if it changed.
    if (source != current_source_) {
        current_source_ = source;
        destroy_subscriptions();    // topics changed; drop the old-source subs...
        reconcile_subscriptions();  // ...and re-subscribe (lazily) whatever current peers still negotiate
        RCLCPP_INFO(this->get_logger(), "Global source switched to %s", source.c_str());
    }

    const bool audio_active = enable_audio_ && request_audio;
    // Independent (client_id) peers negotiate the audio m-line up front (if a mic exists) so toggling it is
    // reneg-free, like the cameras; the legacy peer negotiates audio only when asked (it rebuilds anyway).
    const bool negotiate_audio = client_id.empty() ? audio_active : enable_audio_;

    if (videos.empty() && !audio_active) {
        RCLCPP_INFO(this->get_logger(), "START requested no streams; releasing peer");
        destroy_peer(client_id);
        return;
    }

    // Stream/audio switch on an already-set-up independent peer (its negotiated m-lines unchanged): flip
    // which cameras + audio are pushed live, with no renegotiation/ICE — instant, not a reconnect.
    if (!client_id.empty() && !renegotiate) {
        auto it = peers_.find(client_id);
        if (it != peers_.end() && it->second->with_audio == negotiate_audio) {
            update_peer_active(it->second.get(), videos, audio_active);
            return;
        }
    }

    // Connect (or audio-negotiation change, or the legacy default peer). Independent peers negotiate ALL
    // cameras up front so future switches stay reneg-free; the legacy peer negotiates exactly what it asked.
    std::vector<std::string> all_cams;
    for (auto& cam : cameras_) all_cams.push_back(cam->name);
    const std::vector<std::string> negotiated = client_id.empty() ? videos : all_cams;
    if (!create_peer_transport(client_id, negotiated, videos, negotiate_audio, audio_active)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to start transport for peer");
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
    const SdpMediaCounts counts = count_sdp_media(offer->sdp);
    const bool missing_video = counts.video < ctx->expected_videos;
    const bool missing_audio = ctx->expected_audio && counts.audio == 0;
    const bool missing_data = ctx->expected_data && counts.application == 0;
    if (missing_video || missing_audio || missing_data) {
        RCLCPP_WARN(self->get_logger(),
                    "Dropping incomplete offer for '%s' (video=%u/%u audio=%u%s data=%u%s)",
                    ctx->client_id.empty() ? "(default)" : ctx->client_id.c_str(), counts.video, ctx->expected_videos,
                    counts.audio, ctx->expected_audio ? " required" : "", counts.application,
                    ctx->expected_data ? " required" : "");
        // Do not retry create-offer on this same webrtcbin. GStreamer 1.20 can assert in
        // _add_data_channel_offer() if an incomplete offer is followed by an immediate second offer on the
        // same element. The client offer watchdog will send a renegotiate START, which creates a fresh
        // transport; until then the connect timeout releases this peer.
        g_object_set_data(G_OBJECT(ctx->webrtc), "mars_offering", nullptr);
        g_object_set_data(G_OBJECT(ctx->webrtc), "mars_offered", GINT_TO_POINTER(1));
        gst_webrtc_session_description_free(offer);
        gst_promise_unref(promise);
        return;
    }

    g_signal_emit_by_name(ctx->webrtc, "set-local-description", offer, nullptr);  // fire-and-forget
    g_object_set_data(G_OBJECT(ctx->webrtc), "mars_offering", nullptr);
    g_object_set_data(G_OBJECT(ctx->webrtc), "mars_offered", GINT_TO_POINTER(1));

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
    // Never fake/rewrite candidates. mDNS deferral is handled in deliver_ice(), where we know the peer and
    // can fall back if robot-local STUN does not yield a real LAN srflx candidate.
    return candidate;
}

void WebRTCStreamer::apply_ice(Peer* peer, const std::string& candidate, int mline) {
    g_signal_emit_by_name(peer->webrtc, "add-ice-candidate", mline, candidate.c_str());
}

// The bare topics target the default ("") peer; the *_id topics carry an explicit client_id. Both route
// through these two helpers so the four handlers are thin parse-and-forward wrappers.
void WebRTCStreamer::deliver_answer(const std::string& client_id, const std::string& sdp) {
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it == peers_.end()) {
        RCLCPP_WARN(this->get_logger(), "answer for unknown peer '%s'", client_id.empty() ? "(default)" : client_id.c_str());
        return;
    }
    apply_answer(it->second.get(), sdp);
}

void WebRTCStreamer::deliver_ice(const std::string& client_id, const std::string& candidate, int mline) {
    const std::string prepared = prepare_ice_candidate(candidate);  // keep candidate prep outside the lock
    if (prepared.empty()) {
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it != peers_.end()) {
        Peer* peer = it->second.get();
        const std::string addr = candidate_address(prepared);
        if (is_mdns_address(addr)) {
            if (peer->have_real_remote_ice) {
                RCLCPP_INFO(this->get_logger(), "Dropping late remote mDNS ICE candidate '%s'; real ICE exists",
                            addr.c_str());
                return;
            }
            peer->pending_mdns_ice.emplace_back(mline, prepared);
            if (peer->first_mdns_ice_ns == 0) {
                peer->first_mdns_ice_ns = std::chrono::steady_clock::now().time_since_epoch().count();
            }
            RCLCPP_INFO(this->get_logger(), "Deferring remote mDNS ICE candidate '%s' for local-STUN srflx",
                        addr.c_str());
            return;
        }
        if (!is_mdns_address(addr)) {
            peer->have_real_remote_ice = true;
            if (!peer->pending_mdns_ice.empty()) {
                RCLCPP_INFO(this->get_logger(), "Using real remote ICE candidate '%s'; dropping %zu deferred mDNS candidate(s)",
                            addr.empty() ? "(unknown)" : addr.c_str(), peer->pending_mdns_ice.size());
                peer->pending_mdns_ice.clear();
            }
        }
        apply_ice(peer, prepared, mline);
    }
}

void WebRTCStreamer::on_answer(const std_msgs::msg::String::SharedPtr msg) { deliver_answer("", msg->data); }

void WebRTCStreamer::on_answer_id(const std_msgs::msg::String::SharedPtr msg) {
    try {
        auto j = nlohmann::json::parse(msg->data);
        deliver_answer(j.at("client_id").get<std::string>(), j.at("sdp").get<std::string>());
    } catch (const nlohmann::json::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Bad answer_id: %s", e.what());
    }
}

void WebRTCStreamer::on_ice_in(const std_msgs::msg::String::SharedPtr msg) {
    try {
        auto j = nlohmann::json::parse(msg->data);
        deliver_ice("", j["candidate"].get<std::string>(), j["sdpMLineIndex"].get<int>());
    } catch (const nlohmann::json::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Failed to parse ICE candidate: %s", e.what());
    }
}

void WebRTCStreamer::on_ice_in_id(const std_msgs::msg::String::SharedPtr msg) {
    try {
        auto j = nlohmann::json::parse(msg->data);
        deliver_ice(j.at("client_id").get<std::string>(), j.at("candidate").get<std::string>(),
                    j.at("sdpMLineIndex").get<int>());
    } catch (const nlohmann::json::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Bad ice_in_id: %s", e.what());
    }
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
    if (cid) {
        std::lock_guard<std::mutex> lock(self->peers_mutex_);
        auto it = self->peers_.find(cid);
        if (it != self->peers_.end()) {
            it->second->media_ready = state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED;
        }
    }
    if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
        self->force_keyframe("main");
        self->force_keyframe("arm");
    }
    RCLCPP_INFO(self->get_logger(), "Peer '%s' connection state: %s", (cid && *cid) ? cid : "(default)",
                conn_state_name(state));
}

void WebRTCStreamer::on_diag_channel_open(GstWebRTCDataChannel* channel, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(channel), "client_id"));
    const char* label = nullptr;
    g_object_get(channel, "label", &label, nullptr);
    RCLCPP_INFO(self->get_logger(), "Peer '%s' data channel '%s' open", (cid && *cid) ? cid : "(default)",
                label ? label : "?");

    nlohmann::json hello;
    hello["type"] = "robot-hello";
    hello["client_id"] = cid ? cid : "";
    hello["steady_ns"] = std::chrono::steady_clock::now().time_since_epoch().count();
    gst_webrtc_data_channel_send_string(channel, hello.dump().c_str());
    if (label) g_free(const_cast<char*>(label));
}

void WebRTCStreamer::on_diag_channel_message(GstWebRTCDataChannel* channel, gchar* data, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(channel), "client_id"));
    if (data && std::string(data).find("\"type\":\"browser-ping\"") != std::string::npos) {
        RCLCPP_DEBUG(self->get_logger(), "Peer '%s' data channel browser-ping", (cid && *cid) ? cid : "(default)");
        return;
    }
    RCLCPP_INFO(self->get_logger(), "Peer '%s' data channel <- %s", (cid && *cid) ? cid : "(default)",
                data ? data : "(null)");
}

void WebRTCStreamer::on_diag_channel_close(GstWebRTCDataChannel* channel, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(channel), "client_id"));
    RCLCPP_INFO(self->get_logger(), "Peer '%s' data channel closed", (cid && *cid) ? cid : "(default)");
}


}  // namespace mars_cam
