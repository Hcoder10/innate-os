#pragma once

namespace mars_cam {

// poll_pipeline_health() runs every 200 ms. A FAILED/DISCONNECTED peer is torn down only after this many
// consecutive polls (~15 s), so transient ICE blips can recover without keeping dead transports forever.
constexpr int kTeardownGracePolls = 75;

// A peer that never reaches CONNECTED within this window is released, so dropped offers / failed ICE do
// not leave encoders or subscriptions pinned on.
constexpr double kConnectTimeoutS = 15.0;

// Seconds without RTCP before a CONNECTED peer is considered silently dead. Must be above the browser
// receiver-report cadence for low-bitrate video; Firefox/Linux can stretch to about 5 s.
constexpr double kDefaultRtcpInactivityTimeoutS = 15.0;

// Browser mDNS host candidates arrive before STUN srflx. Give robot-local STUN a brief chance to produce
// a real LAN srflx candidate; if it does not, fall back to handing mDNS candidates to libnice.
constexpr double kMdnsIceFallbackDelayS = 0.5;

}  // namespace mars_cam
