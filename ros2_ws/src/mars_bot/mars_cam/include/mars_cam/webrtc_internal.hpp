// Internal helpers shared across the webrtc_streamer translation units (encode / transport / signaling /
// core). Not part of the public node API — these were the file-local (anonymous-namespace) helpers before
// the .cpp was split; they live here, inline, so every split TU sees one definition without duplication.
#pragma once

#include "mars_cam/webrtc_config.hpp"

#include <gst/gst.h>
#include <gst/rtp/rtp.h>
#include <gst/webrtc/webrtc.h>

#include <arpa/inet.h>
#include <netdb.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <future>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace mars_cam {

class WebRTCStreamer;  // OfferContext only holds a pointer

inline GstWebRTCPeerConnectionState peer_connection_state(GstElement* webrtc) {
    GstWebRTCPeerConnectionState state;
    g_object_get(webrtc, "connection-state", &state, nullptr);
    return state;
}

inline const char* conn_state_name(GstWebRTCPeerConnectionState s) {
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

inline double round1(double v) { return std::round(v * 10.0) / 10.0; }

inline bool wants(const std::vector<std::string>& videos, const std::string& cam) {
    return std::find(videos.begin(), videos.end(), cam) != videos.end();
}

// Each camera gets a fixed SSRC (base + 1-based index), declared in every peer's transport caps so the
// SDP offer carries a=ssrc/msid (built before any RTP has flowed, so webrtcbin can't infer it). All peers
// share a camera's SSRC — fine, since each peer is an independent SRTP transport.
inline guint cam_ssrc_for_index(size_t index) { return 0x1A2B3C00u + static_cast<guint>(index) + 1u; }

// RTP payload type per camera (96, 97, 99, 100, …) — skips 98, which the audio (opus) payloader uses.
inline int cam_pt_for_index(size_t index) {
    const int pt = 96 + static_cast<int>(index);
    return pt >= 98 ? pt + 1 : pt;
}

// Chrome/Firefox often obfuscate their host ICE candidates as "<uuid>.local" mDNS names (one per local
// interface). These parsing helpers are kept for diagnostics and for a future non-destructive fast path:
// if we ever resolve a .local name ourselves, we must ADD the resolved-IP candidate, not replace/drop the
// original. Some Linux browsers do not publish their ephemeral mDNS names, so destructive filtering leaves
// the robot with no host candidate at all and prevents peer-reflexive discovery from rescuing the link.
inline std::string candidate_address(const std::string& cand) {
    std::istringstream iss(cand);
    std::string tok;
    for (int idx = 0; iss >> tok; ++idx) {
        if (idx == 4) return tok;  // candidate:<foundation> <comp> <proto> <prio> <ADDRESS> <port> typ ...
    }
    return "";
}

inline bool is_mdns_address(const std::string& addr) {
    return addr.size() >= 6 && addr.compare(addr.size() - 6, 6, ".local") == 0;
}

// Resolve a name to an IPv4 string, giving up after timeout_ms. getaddrinfo can't be cancelled, so it
// runs on a detached thread that only touches the captured promise (never node state) — safe even if it
// outlives this call: it just finishes the lookup and sets a value nobody reads.
inline std::string resolve_ipv4_timeout(const std::string& host, int timeout_ms) {
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

inline std::string replace_first(const std::string& s, const std::string& from, const std::string& to) {
    const size_t pos = s.find(from);
    return pos == std::string::npos ? s : s.substr(0, pos) + to + s.substr(pos + from.size());
}

// ---- WebRTC "playout-delay" RTP header extension -----------------------------------------------
// Caps the receiver's de-jitter buffer. GStreamer < 1.24 ships no built-in element for this URI, so we
// implement it as a minimal GstRTPHeaderExtension subclass (defined in webrtc_streamer.cpp) and add it to
// each payloader; the matching a=extmap is emitted by webrtcbin from the transport appsrc caps. Wire
// format (WebRTC experiment): 3 bytes = MIN delay (12 bits) | MAX delay (12 bits), 10 ms units.
#define MARS_PLAYOUT_DELAY_URI "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay"
constexpr guint kPlayoutDelayExtId = 14;

GstRTPHeaderExtension* make_playout_delay_ext(guint ext_id, guint min_ms, guint max_ms);

// Context handed to the async create-offer callback. Holds a ref to the peer's webrtcbin (so it
// survives a concurrent teardown) and a copy of the peer's generation token (a shared_ptr, so it stays
// readable even after the Peer is freed): if the peer was torn down/replaced, the generation no longer
// matches and the stale offer is dropped instead of being applied to a vanished connection.
struct OfferContext {
    WebRTCStreamer* self;
    GstElement* webrtc;  // owns a ref, released in offer_context_free
    std::shared_ptr<std::atomic<uint64_t>> gen;
    uint64_t gen_value;
    std::string client_id;
    guint expected_videos = 0;
    bool expected_audio = false;
    bool expected_data = false;
};

inline void offer_context_free(gpointer data) {
    auto* ctx = static_cast<OfferContext*>(data);
    if (ctx->webrtc) {
        gst_object_unref(ctx->webrtc);
    }
    delete ctx;
}

}  // namespace mars_cam
