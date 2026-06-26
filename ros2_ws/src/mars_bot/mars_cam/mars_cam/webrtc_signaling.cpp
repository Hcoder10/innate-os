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
    if (!client_id.empty()) {
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
    const std::string prepared = prepare_ice_candidate(candidate);  // resolve mDNS BEFORE taking the lock
    if (prepared.empty()) {
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it != peers_.end()) {
        apply_ice(it->second.get(), prepared, mline);
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
    if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
        self->force_keyframe("main");
        self->force_keyframe("arm");
    }
    RCLCPP_INFO(self->get_logger(), "Peer '%s' connection state: %s", (cid && *cid) ? cid : "(default)",
                conn_state_name(state));
}


}  // namespace mars_cam
