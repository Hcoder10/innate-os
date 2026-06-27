// Shared WebRTC defaults for robot teleop/debug clients.

const LOCAL_STUN_PORT = 3478;

export const LOCAL_WEBRTC_CONFIG = Object.freeze({
  iceServers: [],
});

/**
 * @param {string | null | undefined} host
 * @returns {string | null}
 */
function stunHost(host) {
  if (!host) return null;
  const trimmed = host.trim();
  if (!trimmed || trimmed === "localhost" || trimmed === "127.0.0.1") return null;
  return trimmed.includes(":") && !trimmed.startsWith("[") ? `[${trimmed}]` : trimmed;
}

/**
 * @param {string | null | undefined} robotHost
 * @returns {RTCConfiguration}
 */
export function createLocalWebRtcConfig(robotHost = null) {
  const host = stunHost(robotHost);
  return host
    ? { iceServers: [{ urls: `stun:${host}:${LOCAL_STUN_PORT}` }] }
    : LOCAL_WEBRTC_CONFIG;
}

/**
 * @param {string | null | undefined} robotHost
 * @returns {RTCPeerConnection}
 */
export function createLocalPeerConnection(robotHost = null) {
  return new RTCPeerConnection(createLocalWebRtcConfig(robotHost));
}

/**
 * Compact candidate description for logs. `.local` means the browser obfuscated a host IP via mDNS.
 * @param {RTCIceCandidate} candidate
 * @returns {string}
 */
export function describeIceCandidate(candidate) {
  return candidate.address
    ? `${candidate.type} ${candidate.address}:${candidate.port} ${candidate.protocol}`
    : candidate.candidate;
}

/**
 * Log any robot-created diagnostic data channel. If this opens while video is black, ICE/DTLS/SCTP are
 * alive and the remaining bug is in RTP/media.
 * @param {RTCPeerConnection} pc
 * @param {(message: string) => void} log
 */
export function wireDiagnosticDataChannels(pc, log = console.log) {
  pc.ondatachannel = (event) => {
    const channel = event.channel;
    log(`[webrtc:data] channel '${channel.label}' received`);
    channel.onopen = () => {
      log(`[webrtc:data] '${channel.label}' open`);
    };
    channel.onmessage = (message) => {
      log(`[webrtc:data] '${channel.label}' <- ${String(message.data)}`);
    };
    channel.onerror = () => log(`[webrtc:data] '${channel.label}' error`);
    channel.onclose = () => {
      log(`[webrtc:data] '${channel.label}' closed`);
    };
  };
}
