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
    // Audio is encoded ONCE in the shared mic pipeline and fanned out, so the transport just needs an
    // appsrc m-line; its opus RTP caps are set programmatically in create_peer_transport (before linking).
    if (with_audio) {
        desc += "appsrc name=rtp_audio is-live=true format=time do-timestamp=false ";
    }
    return desc;
}

// Apply RTP caps + live/leaky tuning to a transport appsrc, then request the next webrtcbin sink pad and
// link it. Consumes `caps`. Returns true once linked (the caller keeps the appsrc ref in peer->rtp).
// do-timestamp=TRUE re-stamps each forwarded buffer with this transport pipeline's running-time: the
// encode pipeline is separate with its own base-time, so its PTS look future-dated here and webrtcbin
// would hold them. The RTP header timestamps the receiver plays back by are the payloader's, untouched.
bool WebRTCStreamer::link_rtp_appsrc(GstElement* webrtc, GstElement* appsrc, GstCaps* caps, guint64 max_bytes) {
    g_object_set(appsrc, "caps", caps, "is-live", TRUE, "format", GST_FORMAT_TIME, "do-timestamp", TRUE, "block",
                 FALSE, "leaky-type", 2 /* downstream */, "max-bytes", max_bytes, nullptr);
    gst_caps_unref(caps);
    GstPad* srcpad = gst_element_get_static_pad(appsrc, "src");
    GstPad* sinkpad = gst_element_request_pad_simple(webrtc, "sink_%u");
    const bool linked = srcpad && sinkpad && gst_pad_link(srcpad, sinkpad) == GST_PAD_LINK_OK;
    if (srcpad) gst_object_unref(srcpad);
    if (sinkpad) gst_object_unref(sinkpad);
    return linked;
}

Peer* WebRTCStreamer::create_peer_transport(const std::string& client_id, const std::vector<std::string>& negotiated,
                                            const std::vector<std::string>& active, bool with_audio, bool audio_active) {
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
    peer->audio_active = with_audio && audio_active;  // can only be active if the m-line was negotiated
    peer->created_ns = std::chrono::steady_clock::now().time_since_epoch().count();
    peer->webrtc = gst_bin_get_by_name(GST_BIN(pipeline), "webrtc");
    if (!peer->webrtc) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing webrtcbin");
        return nullptr;  // ~Peer() tears down the pipeline
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
        // Set the (VP8) caps, then request the next webrtcbin sink pad and link — so the transceiver is
        // built from the real caps, in m-line order (sink_0, sink_1, …).
        if (!link_rtp_appsrc(peer->webrtc, src, caps, 2 * 1024 * 1024)) {
            RCLCPP_ERROR(this->get_logger(), "Failed to link rtp_%s to webrtcbin", v.c_str());
            gst_object_unref(src);
            ok = false;
            break;
        }
        peer->rtp[v] = src;  // keep the ref (camera name -> transport appsrc)
    }
    // Audio (if negotiated): set the opus RTP caps on the audio appsrc (caps-before-link, same as video)
    // and link it as the LAST m-line. The shared mic pipeline fans its RTP into this appsrc.
    if (ok && peer->with_audio) {
        GstElement* asrc = gst_bin_get_by_name(GST_BIN(pipeline), "rtp_audio");
        if (asrc) {
            GstCaps* caps =
                gst_caps_new_simple("application/x-rtp", "media", G_TYPE_STRING, "audio", "encoding-name",
                                    G_TYPE_STRING, "OPUS", "clock-rate", G_TYPE_INT, 48000, "encoding-params",
                                    G_TYPE_STRING, "2", "payload", G_TYPE_INT, 98, nullptr);
            if (link_rtp_appsrc(peer->webrtc, asrc, caps, 1 * 1024 * 1024)) {
                peer->rtp["audio"] = asrc;  // keep the ref
            } else {
                RCLCPP_WARN(this->get_logger(), "Failed to link audio appsrc; continuing video-only");
                gst_object_unref(asrc);
                peer->with_audio = false;
                peer->audio_active = false;
            }
        } else {
            peer->with_audio = false;
            peer->audio_active = false;
        }
    }
    if (!ok) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing/failed an rtp appsrc");
        return nullptr;  // ~Peer() tears down the pipeline + the rtp appsrcs stored so far
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

    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_ASYNC) {
        ret = gst_element_get_state(pipeline, nullptr, nullptr, 3 * GST_SECOND);
    }
    if (ret == GST_STATE_CHANGE_FAILURE) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline failed to reach PLAYING (audio=%s)",
                     with_audio ? "on" : "off");
        return nullptr;  // ~Peer() tears down the pipeline
    }

    Peer* raw = peer.get();
    peers_[client_id] = std::move(peer);
    // want-count gates the encoders; count only ACTIVE (pushed) cameras, not merely negotiated ones, so a
    // peer that negotiated several but is viewing one doesn't pin the others' encoders on.
    for (const auto& v : active) {
        if (CameraEncoder* c = find_camera(v)) c->want.fetch_add(1, std::memory_order_relaxed);
    }
    if (raw->audio_active) {
        want_audio_.fetch_add(1, std::memory_order_relaxed);
        reconcile_audio();  // opens the mic — safe under the lock (opening never joins the fan-out thread)
    }
    reconcile_subscriptions();  // this peer just negotiated its cameras — make sure they're subscribed

    // The offer is created from on-negotiation-needed (fires once the transceivers are set up).
    RCLCPP_INFO(this->get_logger(), "Peer '%s' transport PLAYING (negotiated=%zu, active=%zu, audio=%s)",
                client_id.empty() ? "(default)" : client_id.c_str(), negotiated.size(), active.size(),
                raw->audio_active ? "on" : (raw->with_audio ? "negotiated/off" : "off"));
    return raw;
}

void WebRTCStreamer::update_peer_active(Peer* peer, const std::vector<std::string>& active, bool audio_active) {
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

    // Audio toggles the same way (only if the m-line was negotiated). Opening the mic here is safe under
    // the lock; closing it (want_audio_ -> 0) is deferred to the health poll, which runs without the lock,
    // because NULL-ing the audio pipeline joins the fan-out thread that also takes peers_mutex_.
    const bool audio_now = peer->with_audio && audio_active;
    if (audio_now != peer->audio_active) {
        if (audio_now) {
            want_audio_.fetch_add(1, std::memory_order_relaxed);
            peer->audio_active = true;
            reconcile_audio();  // open the mic now
        } else {
            want_audio_.fetch_sub(1, std::memory_order_relaxed);
            peer->audio_active = false;  // reconcile_audio() (poll_pipeline_health) closes the mic if 0
        }
    }
    if (peer->audio_active) summary += (summary.empty() ? "" : "+") + std::string("audio");

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
    // Mirror create/update: the want-count tracks ACTIVE streams, so release exactly what this peer held.
    for (const auto& v : p->active) {
        if (CameraEncoder* c = find_camera(v)) c->want.fetch_sub(1, std::memory_order_relaxed);
    }
    if (p->audio_active) want_audio_.fetch_sub(1, std::memory_order_relaxed);  // mic closed by the health poll

    RCLCPP_INFO(this->get_logger(), "Released peer '%s'", client_id.empty() ? "(default)" : client_id.c_str());
    peers_.erase(it);  // ~Peer() NULLs the transport (joining its streaming threads) and releases the refs
    reconcile_subscriptions();  // last peer wanting a camera may have just left — drop its sub if so
}


}  // namespace mars_cam
