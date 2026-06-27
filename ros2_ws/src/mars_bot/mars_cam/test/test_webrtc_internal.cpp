// Unit tests for the GStreamer/ROS-free helpers in webrtc_internal.hpp. These are the pieces of the
// streamer that are pure logic (PT/SSRC assignment, ICE/mDNS candidate parsing, status
// rounding) and so are testable without a live pipeline or node. The pipeline/webrtcbin paths stay
// integration-tested by test/webrtc_consumer.py.
#include <gtest/gtest.h>

#include "mars_cam/webrtc_internal.hpp"

using namespace mars_cam;

// ---- RTP payload types: 96, 97, then skip 98 (reserved for opus audio), 99, 100, … ----
TEST(CamPt, SkipsAudioPayloadType98) {
    EXPECT_EQ(cam_pt_for_index(0), 96);
    EXPECT_EQ(cam_pt_for_index(1), 97);
    EXPECT_EQ(cam_pt_for_index(2), 99);   // 98 is opus
    EXPECT_EQ(cam_pt_for_index(3), 100);
    EXPECT_NE(cam_pt_for_index(2), 98);
}

// ---- SSRCs: fixed, unique per camera, 1-based off the base ----
TEST(CamSsrc, UniquePerCamera) {
    EXPECT_EQ(cam_ssrc_for_index(0), 0x1A2B3C01u);
    EXPECT_EQ(cam_ssrc_for_index(1), 0x1A2B3C02u);
    EXPECT_NE(cam_ssrc_for_index(0), cam_ssrc_for_index(1));
}

// ---- mDNS detection: browsers obfuscate host candidates as "<uuid>.local" ----
TEST(Mdns, DetectsDotLocalSuffix) {
    EXPECT_TRUE(is_mdns_address("a1b2c3d4.local"));
    EXPECT_FALSE(is_mdns_address("192.168.1.5"));
    EXPECT_FALSE(is_mdns_address("fe80::1"));
    EXPECT_FALSE(is_mdns_address("local"));  // shorter than ".local"
    EXPECT_FALSE(is_mdns_address(""));
}

// ---- candidate_address: pull the 5th whitespace token (the connection address) ----
TEST(CandidateAddress, ExtractsConnectionAddress) {
    // candidate:<foundation> <comp> <proto> <prio> <ADDRESS> <port> typ ...
    EXPECT_EQ(candidate_address("candidate:1 1 UDP 2122260223 192.168.1.5 54321 typ host"), "192.168.1.5");
    EXPECT_EQ(candidate_address("candidate:2 1 UDP 12345 abcd.local 9 typ host"), "abcd.local");
    EXPECT_EQ(candidate_address("candidate:9 1 UDP 1 fe80::abcd 5000 typ host"), "fe80::abcd");
    EXPECT_EQ(candidate_address("too short"), "");  // no 5th token
}

// ---- replace_first: utility for non-destructive candidate diagnostics/augmentation ----
TEST(ReplaceFirst, ReplacesOnlyTheFirstOccurrence) {
    EXPECT_EQ(replace_first("a.local then b.local", ".local", ".ip"), "a.ip then b.local");
    EXPECT_EQ(replace_first("no match here", "xyz", "Q"), "no match here");
    EXPECT_EQ(replace_first("", "x", "y"), "");
}

// ---- round1: status fps/rtt rounding to one decimal ----
TEST(Round1, RoundsToOneDecimal) {
    EXPECT_DOUBLE_EQ(round1(1.24), 1.2);
    EXPECT_DOUBLE_EQ(round1(1.27), 1.3);
    EXPECT_DOUBLE_EQ(round1(0.0), 0.0);
    EXPECT_DOUBLE_EQ(round1(14.46), 14.5);
}

// ---- wants: is a camera in this peer's negotiated/active set ----
TEST(Wants, Membership) {
    const std::vector<std::string> v{"main", "arm"};
    EXPECT_TRUE(wants(v, "main"));
    EXPECT_TRUE(wants(v, "arm"));
    EXPECT_FALSE(wants(v, "wrist"));
    EXPECT_FALSE(wants({}, "main"));
}

// ---- conn_state_name: webrtcbin connection-state enum -> status string ----
TEST(ConnStateName, MapsKnownStates) {
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_NEW), "new");
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED), "connected");
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_FAILED), "failed");
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED), "closed");
}

// ---- resolve_ipv4_timeout: a numeric IP resolves to itself with no real DNS lookup (deterministic) ----
TEST(ResolveIpv4, ResolvesNumericLiteralWithoutNetwork) {
    EXPECT_EQ(resolve_ipv4_timeout("127.0.0.1", 500), "127.0.0.1");
}

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
