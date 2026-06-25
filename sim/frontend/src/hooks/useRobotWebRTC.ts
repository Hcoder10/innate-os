import { useCallback, useEffect, useRef, useState } from "react";

const WEBRTC_START_TOPIC = "/webrtc/start";
const WEBRTC_OFFER_TOPIC = "/webrtc/offer";
const WEBRTC_ANSWER_TOPIC = "/webrtc/answer";
const WEBRTC_ICE_IN_TOPIC = "/webrtc/ice_in";
const WEBRTC_ICE_OUT_TOPIC = "/webrtc/ice_out";
// Additive: tells the server which cameras to encode (lazy encoding). A server
// that doesn't support it (e.g. the robot streamer) simply ignores the topic.
const WEBRTC_ACTIVE_STREAMS_TOPIC = "/webrtc/active_streams";

type WebRTCSource = "live" | "episode_replay";

// One video camera advertised by whatever WebRTC server we connected to. `mid`
// is the SDP media id (the transceiver key); `name` is the camera identity the
// server labelled the track with (SDP msid), falling back to a positional name.
export interface CameraInfo {
  mid: string;
  name: string;
}

interface UseRobotWebRTCOptions {
  enabled: boolean;
  wsUrl: string;
  source?: WebRTCSource;
}

interface UseRobotWebRTCReturn {
  cameras: CameraInfo[];
  streamsByMid: Record<string, MediaStream>;
  hasMedia: boolean;
  isConnecting: boolean;
  error: string | null;
  reconnect: () => void;
  setActiveCameras: (cameras: string[]) => void;
}

const CONNECTION_TIMEOUT_MS = 30000;
const START_SIGNAL_DELAY_MS = 100;

// Derive the video cameras (mid + name) straight from the offer SDP, so the UI
// adapts to whatever the connected source advertises — robot or sim, 1..N cams.
// Identity comes from the track label (`a=msid:<stream> <name>`, or the older
// `a=ssrc:<id> msid:<stream> <name>` form); absent that we fall back to the mid.
function parseVideoCameras(sdp: string): CameraInfo[] {
  const cameras: CameraInfo[] = [];
  let inVideo = false;
  let mid: string | null = null;
  let name: string | null = null;

  const flush = () => {
    if (inVideo && mid !== null) {
      cameras.push({ mid, name: name ?? `camera_${mid}` });
    }
    mid = null;
    name = null;
  };

  for (const raw of sdp.split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("m=")) {
      flush();
      inVideo = line.startsWith("m=video");
    } else if (inVideo && line.startsWith("a=mid:")) {
      mid = line.slice("a=mid:".length).trim();
    } else if (inVideo && name === null && line.startsWith("a=msid:")) {
      const parts = line.split(/\s+/);
      if (parts.length >= 2) name = parts[parts.length - 1];
    } else if (inVideo && name === null && line.startsWith("a=ssrc:")) {
      const m = line.match(/\bmsid:\S+\s+(\S+)/);
      if (m) name = m[1];
    }
  }
  flush();
  return cameras;
}

export function useRobotWebRTC({
  enabled,
  wsUrl,
  source = "live",
}: UseRobotWebRTCOptions): UseRobotWebRTCReturn {
  const [cameras, setCameras] = useState<CameraInfo[]>([]);
  const [streamsByMid, setStreamsByMid] = useState<Record<string, MediaStream>>(
    {},
  );
  const [hasMedia, setHasMedia] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconnectCount, setReconnectCount] = useState(0);
  const hasMediaRef = useRef(false);
  const wsRef = useRef<WebSocket | null>(null);

  const setActiveCameras = useCallback((names: string[]) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify({
          op: "publish",
          topic: WEBRTC_ACTIVE_STREAMS_TOPIC,
          msg: { data: JSON.stringify({ cameras: names }) },
        }),
      );
    }
  }, []);

  const reconnect = useCallback(() => {
    setReconnectCount((count) => count + 1);
  }, []);

  useEffect(() => {
    hasMediaRef.current = hasMedia;
  }, [hasMedia]);

  useEffect(() => {
    const resetState = () => {
      setCameras([]);
      setStreamsByMid({});
      setHasMedia(false);
    };

    if (!enabled) {
      resetState();
      setIsConnecting(false);
      setError(null);
      return;
    }

    let isMounted = true;
    let ws: WebSocket | null = null;
    let pc: RTCPeerConnection | null = null;
    let connectionTimeout: number | null = null;
    let startSignalTimer: number | null = null;
    let processingOffer = false;
    let remoteDescriptionSet = false;
    const iceCandidateQueue: RTCIceCandidateInit[] = [];

    const clearTimers = () => {
      if (connectionTimeout !== null) {
        window.clearTimeout(connectionTimeout);
        connectionTimeout = null;
      }
      if (startSignalTimer !== null) {
        window.clearTimeout(startSignalTimer);
        startSignalTimer = null;
      }
    };

    const closeConnections = () => {
      clearTimers();

      if (pc) {
        pc.ontrack = null;
        pc.onicecandidate = null;
        pc.oniceconnectionstatechange = null;
        pc.close();
        pc = null;
      }

      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
        ws = null;
      }
      wsRef.current = null;
    };

    const sendRosbridgeMessage = (payload: object) => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(payload));
      }
    };

    const publishTopic = (topic: string, msg: object) => {
      sendRosbridgeMessage({ op: "publish", topic, msg });
    };

    const createPeerConnection = () => {
      pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
      });

      remoteDescriptionSet = false;
      iceCandidateQueue.length = 0;

      pc.onicecandidate = (event) => {
        if (!event.candidate) {
          return;
        }

        publishTopic(WEBRTC_ICE_IN_TOPIC, {
          data: JSON.stringify({
            candidate: event.candidate.candidate,
            sdpMLineIndex: event.candidate.sdpMLineIndex,
            sdpMid: event.candidate.sdpMid,
          }),
        });
      };

      pc.ontrack = (event) => {
        if (!isMounted || event.track.kind !== "video") {
          return;
        }

        const mid = event.transceiver?.mid;
        if (!mid) {
          return;
        }

        const stream = new MediaStream([event.track]);
        setStreamsByMid((prev) => ({ ...prev, [mid]: stream }));
        setHasMedia(true);
        setIsConnecting(false);
        setError(null);
        clearTimers();
      };
    };

    const processOffer = async (offerSdp: string) => {
      if (!pc || processingOffer || pc.signalingState !== "stable") {
        return;
      }

      processingOffer = true;
      try {
        await pc.setRemoteDescription({ type: "offer", sdp: offerSdp });
        remoteDescriptionSet = true;

        // The advertised camera set lives entirely in the offer.
        setCameras(parseVideoCameras(offerSdp));

        for (const candidate of iceCandidateQueue) {
          try {
            await pc.addIceCandidate(candidate);
          } catch {
            // Ignore malformed ICE candidates.
          }
        }
        iceCandidateQueue.length = 0;

        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        publishTopic(WEBRTC_ANSWER_TOPIC, { data: answer.sdp ?? "" });
      } catch (offerError) {
        console.error("[WebRTC] Failed to process offer:", offerError);
      } finally {
        processingOffer = false;
      }
    };

    const processIceOut = async (rawIceData: string) => {
      if (!pc) {
        return;
      }

      try {
        const parsed = JSON.parse(rawIceData) as {
          candidate?: string;
          sdpMLineIndex?: number;
          sdpMid?: string;
        };

        if (!parsed.candidate) {
          return;
        }

        const candidate: RTCIceCandidateInit = {
          candidate: parsed.candidate,
          sdpMLineIndex: parsed.sdpMLineIndex ?? 0,
          sdpMid: parsed.sdpMid ?? undefined,
        };

        if (!remoteDescriptionSet) {
          iceCandidateQueue.push(candidate);
          return;
        }

        await pc.addIceCandidate(candidate);
      } catch {
        // ICE parsing failures are usually transient and can be ignored.
      }
    };

    const connectWebSocket = () => {
      closeConnections();
      resetState();
      setError(null);
      setIsConnecting(true);

      connectionTimeout = window.setTimeout(() => {
        if (!isMounted) {
          return;
        }
        setError("Timed out while waiting for the WebRTC video stream.");
        setIsConnecting(false);
      }, CONNECTION_TIMEOUT_MS);

      ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!isMounted) {
          return;
        }

        createPeerConnection();

        sendRosbridgeMessage({ op: "subscribe", topic: WEBRTC_OFFER_TOPIC });
        sendRosbridgeMessage({ op: "subscribe", topic: WEBRTC_ICE_OUT_TOPIC });

        startSignalTimer = window.setTimeout(() => {
          const sourceForRobot =
            source === "episode_replay" ? "replay" : source;
          publishTopic(WEBRTC_START_TOPIC, {
            data: JSON.stringify({ source: sourceForRobot }),
          });
          startSignalTimer = null;
        }, START_SIGNAL_DELAY_MS);
      };

      ws.onmessage = async (event) => {
        try {
          const data = JSON.parse(event.data as string);
          if (data.op !== "publish" || typeof data.topic !== "string") {
            return;
          }

          if (data.topic === WEBRTC_OFFER_TOPIC) {
            const offerSdp = data.msg?.data ?? data.data;
            if (typeof offerSdp === "string" && offerSdp.length > 0) {
              await processOffer(offerSdp);
            }
            return;
          }

          if (data.topic === WEBRTC_ICE_OUT_TOPIC) {
            const rawIceData = data.msg?.data ?? data.data;
            if (typeof rawIceData === "string" && rawIceData.length > 0) {
              await processIceOut(rawIceData);
            }
          }
        } catch {
          // Ignore non-JSON messages from ROSBridge.
        }
      };

      ws.onerror = () => {
        if (!isMounted) {
          return;
        }
        setError("Failed to connect to the signaling WebSocket.");
      };

      ws.onclose = () => {
        if (!isMounted) {
          return;
        }

        if (!hasMediaRef.current) {
          setError("Signaling WebSocket closed.");
          setIsConnecting(false);
        }
      };
    };

    connectWebSocket();

    return () => {
      isMounted = false;
      closeConnections();
      resetState();
      setIsConnecting(false);
    };
  }, [enabled, wsUrl, source, reconnectCount]);

  return {
    cameras,
    streamsByMid,
    hasMedia,
    isConnecting,
    error,
    reconnect,
    setActiveCameras,
  };
}
