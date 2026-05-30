/**
 * A Chat component replicating the style you requested.
 */
import { useState, useEffect, useRef } from "react";
import styled from "styled-components";
import { IoSend, IoStop } from "react-icons/io5";
import { RobotGroupedBubble } from "./RobotGroupedBubble";
import { groupMessages, Message, DisplayMessage } from "../utils/groupMessages";
import { CartesiaClient } from "@cartesia/cartesia-js";
import type CartesiaWebsocket from "@cartesia/cartesia-js/wrapper/Websocket";
import { stopAgentDirect } from "../services/rosbridgeService";

const ChatContainer = styled.div`
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  font-family: ${({ theme }) => theme.fonts.mono};
  background: ${({ theme }) => theme.colors.background};
`;

const MessagesWrapper = styled.div`
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
`;

const InteractionMessage = styled.div`
  display: flex;
  flex-direction: column;
  gap: 6px;
`;

const InteractionLabel = styled.span<{ $tone: "mars" | "user" | "skill" | "system" }>`
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: ${({ $tone }) => {
    if ($tone === "mars") return "#f97316";
    if ($tone === "skill") return "#a855f7";
    if ($tone === "system") return "#3b82f6";
    return "#6b7280";
  }};
`;

const InteractionBody = styled.div<{
  $tone: "mars" | "user" | "skill" | "system";
  $isError?: boolean;
}>`
  padding: 12px;
  border-left: 2px solid
    ${({ $tone, $isError }) => {
      if ($isError) return "#ef4444";
      if ($tone === "mars") return "#f97316";
      if ($tone === "skill") return "#a855f7";
      if ($tone === "system") return "#3b82f6";
      return "#374151";
    }};
  background: ${({ $tone, $isError }) => {
    if ($isError) return "rgba(127, 29, 29, 0.22)";
    if ($tone === "mars") return "rgba(154, 52, 18, 0.1)";
    if ($tone === "skill") return "rgba(88, 28, 135, 0.2)";
    if ($tone === "system") return "rgba(17, 24, 39, 0.4)";
    return "#000";
  }};
  color: ${({ $tone, $isError }) => {
    if ($isError) return "#fecaca";
    if ($tone === "skill") return "#d8b4fe";
    if ($tone === "system" || $tone === "mars") return "#d1d5db";
    return "#e5e7eb";
  }};
  font-size: 12px;
  line-height: 1.55;
  max-width: 100%;
  overflow-wrap: anywhere;
`;

const SkillLine = styled.div`
  display: flex;
  align-items: center;
  gap: 6px;
`;

const SkillStatusText = styled.span<{ $status?: string }>`
  margin-left: auto;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: ${({ $status, theme }) =>
    $status === "failed"
      ? "#f87171"
      : $status === "interrupted"
        ? "#facc15"
        : $status === "completed"
          ? theme.colors.primary
          : "#c084fc"};
`;

const SkillPulse = styled.div<{ $active?: boolean }>`
  width: 8px;
  height: 8px;
  border-radius: 999px;
  flex: 0 0 auto;
  background: #a855f7;
  box-shadow: 0 0 6px rgba(168, 85, 247, 0.8);
  animation: ${({ $active }) =>
    $active ? "skill-pulse 1.4s ease-in-out infinite" : "none"};

  @keyframes skill-pulse {
    0%, 100% {
      opacity: 0.45;
      transform: scale(0.9);
    }
    50% {
      opacity: 1;
      transform: scale(1);
    }
  }
`;

const InputArea = styled.div`
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 16px;
  gap: 8px;
  border-top: 1px solid ${({ theme }) => theme.colors.foreground};
`;

const TextInput = styled.input`
  flex: 1;
  border: none;
  padding: 10px;
  outline: none;
  background: ${({ theme }) => theme.colors.secondary};
  font-size: 13px;
  font-family: ${({ theme }) => theme.fonts.mono};
  color: ${({ theme }) => theme.colors.foreground};
  border-bottom: 2px solid transparent;

  &:focus {
    border-bottom-color: ${({ theme }) => theme.colors.primary};
  }

  ::placeholder {
    color: ${({ theme }) => theme.colors.muted};
  }
`;

const SendButton = styled.button`
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: ${({ theme }) => theme.colors.foreground};
  background: transparent;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  border-radius: 50%;
  transition: all 0.2s;

  &:hover {
    background: ${({ theme }) => theme.colors.foreground};
    color: ${({ theme }) => theme.colors.background};
  }
`;

const StopButton = styled.button`
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 50%;
  transition: all 0.2s;
  border: 1px solid ${({ theme }) => theme.colors.foreground};
  background: transparent;
  color: ${({ theme }) => theme.colors.foreground};

  &:hover {
    background: #dc2626;
    border-color: #dc2626;
    color: white;
  }
`;

const CHAT_IN_TOPIC = "/brain/chat_in";
const CHAT_OUT_TOPIC = "/brain/chat_out";
const SKILL_STATUS_UPDATE_TOPIC = "/brain/skill_status_update";

const VALID_CHAT_SENDERS = new Set<Message["sender"]>([
  "user",
  "robot",
  "robot_thoughts",
  "robot_anticipation",
  "system",
  "vision_agent_output",
  "task_activated",
]);

const parseRosbridgeChatMessage = (payload: unknown): Message | null => {
  let parsedPayload: unknown = payload;

  if (typeof parsedPayload === "string") {
    try {
      parsedPayload = JSON.parse(parsedPayload);
    } catch {
      return null;
    }
  }

  if (!parsedPayload || typeof parsedPayload !== "object") {
    return null;
  }

  const chatMessage = parsedPayload as {
    sender?: unknown;
    text?: unknown;
    timestamp?: unknown;
    timestamp_sec?: unknown;
    taskStatus?: unknown;
    primitiveName?: unknown;
    primitiveId?: unknown;
    skillId?: unknown;
    failureReason?: unknown;
  };

  if (
    typeof chatMessage.sender !== "string" ||
    !VALID_CHAT_SENDERS.has(chatMessage.sender as Message["sender"]) ||
    typeof chatMessage.text !== "string"
  ) {
    return null;
  }

  const timestamp =
    typeof chatMessage.timestamp === "number"
      ? chatMessage.timestamp
      : typeof chatMessage.timestamp_sec === "number"
        ? chatMessage.timestamp_sec
        : Date.now() / 1000;

  return {
    sender: chatMessage.sender as Message["sender"],
    text: chatMessage.text,
    timestamp,
    taskStatus:
      typeof chatMessage.taskStatus === "string"
        ? chatMessage.taskStatus
        : undefined,
    primitiveName:
      typeof chatMessage.primitiveName === "string"
        ? chatMessage.primitiveName
        : undefined,
    primitiveId:
      typeof chatMessage.primitiveId === "string"
        ? chatMessage.primitiveId
        : undefined,
    skillId:
      typeof chatMessage.skillId === "string" ? chatMessage.skillId : undefined,
    failureReason:
      typeof chatMessage.failureReason === "string"
        ? chatMessage.failureReason
        : undefined,
  };
};

const appendUniqueMessage = (
  previous: Message[],
  incoming: Message,
): Message[] => {
  const duplicateExists = previous.some(
    (message) =>
      message.sender === incoming.sender &&
      message.text === incoming.text &&
      message.timestamp === incoming.timestamp,
  );

  if (duplicateExists) {
    return previous;
  }

  const nextMessages = [...previous, incoming];
  nextMessages.sort((a, b) => a.timestamp - b.timestamp);
  return nextMessages;
};

const parseSkillStatusMessage = (payload: unknown): Message | null => {
  let parsedPayload: unknown = payload;

  if (typeof parsedPayload === "string") {
    try {
      parsedPayload = JSON.parse(parsedPayload);
    } catch {
      return null;
    }
  }

  if (!parsedPayload || typeof parsedPayload !== "object") {
    return null;
  }

  const statusPayload = parsedPayload as {
    primitive_name?: unknown;
    primitive_id?: unknown;
    skill_name?: unknown;
    skill_id?: unknown;
    status?: unknown;
    timestamp?: unknown;
    reason?: unknown;
  };
  const text =
    typeof statusPayload.primitive_name === "string"
      ? statusPayload.primitive_name
      : typeof statusPayload.skill_name === "string"
        ? statusPayload.skill_name
        : typeof statusPayload.skill_id === "string"
          ? statusPayload.skill_id
          : "";

  if (!text || typeof statusPayload.status !== "string") {
    return null;
  }

  return {
    sender: "task_activated",
    text,
    timestamp:
      typeof statusPayload.timestamp === "number"
        ? statusPayload.timestamp
        : Date.now() / 1000,
    taskStatus: statusPayload.status,
    primitiveName: text,
    primitiveId:
      typeof statusPayload.primitive_id === "string"
        ? statusPayload.primitive_id
        : undefined,
    skillId:
      typeof statusPayload.skill_id === "string"
        ? statusPayload.skill_id
        : undefined,
    failureReason:
      typeof statusPayload.reason === "string" ? statusPayload.reason : undefined,
  };
};

const mergeSkillStatusMessage = (
  previous: Message[],
  incoming: Message,
): Message[] => {
  if (!incoming.primitiveId) {
    return appendUniqueMessage(previous, incoming);
  }

  const nextMessages = [...previous];
  const targetIndex = [...nextMessages]
    .reverse()
    .findIndex((message) => {
      if (message.sender !== "task_activated") {
        return false;
      }
      return message.primitiveId === incoming.primitiveId;
    });

  if (targetIndex < 0) {
    return appendUniqueMessage(previous, incoming);
  }

  const forwardIndex = nextMessages.length - 1 - targetIndex;
  nextMessages[forwardIndex] = {
    ...nextMessages[forwardIndex],
    ...incoming,
    timestamp: nextMessages[forwardIndex].timestamp,
  };
  nextMessages.sort((a, b) => a.timestamp - b.timestamp);
  return nextMessages;
};

const taskStatusLabel = (status?: string) => {
  switch (status) {
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "interrupted":
      return "stopped";
    default:
      return "running";
  }
};

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isScrolledToBottom, setIsScrolledToBottom] = useState(true);
  const wsRef = useRef<WebSocket | null>(null);
  const [isSpeaking, setIsSpeaking] = useState(false);
  const audioContextRef = useRef<AudioContext | null>(null);
  const cartesiaRef = useRef<CartesiaClient | null>(null);
  const websocketRef = useRef<CartesiaWebsocket | null>(null);
  const audioBuffersRef = useRef<Float32Array[]>([]);
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const [audioQueue, setAudioQueue] = useState<Float32Array[]>([]);
  const isPlayingRef = useRef<boolean>(false);
  const useDirectRobot = import.meta.env.VITE_DIRECT_ROBOT === "true";
  const robotWsUrl = import.meta.env.VITE_ROBOT_WS_URL ?? "ws://localhost:9090";
  const backendWsBaseUrl =
    import.meta.env.VITE_WS_BASE_URL ?? "ws://localhost:8000";
  const handleScroll = () => {
    if (containerRef.current) {
      const { scrollTop, scrollHeight, clientHeight } = containerRef.current;
      setIsScrolledToBottom(scrollHeight - scrollTop - clientHeight < 10);
    }
  };
  useEffect(() => {
    // If there's a socket and it's not fully closed, skip making a new one.
    if (
      wsRef.current &&
      wsRef.current.readyState !== WebSocket.CLOSED &&
      wsRef.current.readyState !== WebSocket.CLOSING
    ) {
      return;
    }

    // Use anonymous user ID
    const userId = "anonymous";
    // Add user ID as query parameter
    const wsUrl = useDirectRobot
      ? robotWsUrl
      : `${backendWsBaseUrl}/ws/chat?user_id=${encodeURIComponent(userId)}`;
    let reconnectTimeout: number | null = null;
    let reconnectAttempts = 0;
    let shouldReconnect = true;
    const MAX_RECONNECT_ATTEMPTS = 5;
    const BASE_RECONNECT_DELAY_MS = 2000;
    const MAX_RECONNECT_DELAY_MS = 15000;

    const clearReconnectTimeout = () => {
      if (reconnectTimeout !== null) {
        window.clearTimeout(reconnectTimeout);
        reconnectTimeout = null;
      }
    };

    const scheduleReconnect = () => {
      if (!shouldReconnect || reconnectTimeout !== null) {
        return;
      }
      if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        return;
      }

      reconnectAttempts += 1;
      const delayMs = Math.min(
        BASE_RECONNECT_DELAY_MS * reconnectAttempts,
        MAX_RECONNECT_DELAY_MS,
      );
      reconnectTimeout = window.setTimeout(() => {
        reconnectTimeout = null;
        connectWebSocket();
      }, delayMs);
    };

    // Create a function to establish the WebSocket connection
    const connectWebSocket = () => {
      try {
        const socket = new WebSocket(wsUrl);
        wsRef.current = socket;

        socket.onopen = () => {
          reconnectAttempts = 0;
          clearReconnectTimeout();
          if (useDirectRobot) {
            socket.send(
              JSON.stringify({ op: "subscribe", topic: CHAT_OUT_TOPIC }),
            );
            socket.send(
              JSON.stringify({ op: "subscribe", topic: CHAT_IN_TOPIC }),
            );
            socket.send(
              JSON.stringify({
                op: "subscribe",
                topic: SKILL_STATUS_UPDATE_TOPIC,
              }),
            );
          }
        };

        socket.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);

            if (useDirectRobot) {
              if (
                data.op === "publish" &&
                (data.topic === CHAT_OUT_TOPIC || data.topic === CHAT_IN_TOPIC)
              ) {
                const parsedMessage = parseRosbridgeChatMessage(
                  data.msg?.data ?? data.msg ?? data.data,
                );
                if (parsedMessage) {
                  setMessages((prev) =>
                    parsedMessage.sender === "task_activated"
                      ? mergeSkillStatusMessage(prev, parsedMessage)
                      : appendUniqueMessage(prev, parsedMessage),
                  );
                }
              } else if (
                data.op === "publish" &&
                data.topic === SKILL_STATUS_UPDATE_TOPIC
              ) {
                const parsedMessage = parseSkillStatusMessage(
                  data.msg?.data ?? data.msg ?? data.data,
                );
                if (parsedMessage) {
                  setMessages((prev) =>
                    mergeSkillStatusMessage(prev, parsedMessage),
                  );
                }
              }
              return;
            }

            // Handle error messages
            if (data.error) {
              setMessages((prev) => [
                ...prev,
                {
                  sender: data.sender || "system",
                  text: data.text,
                  timestamp: data.timestamp || Date.now() / 1000,
                  isError: true,
                },
              ]);
              return;
            }

            if (data.sender && data.text) {
              const parsedMessage = parseRosbridgeChatMessage({
                sender: data.sender,
                text: data.text,
                timestamp: data.timestamp,
                taskStatus: data.taskStatus,
                primitiveId: data.primitiveId,
                skillId: data.skillId,
                failureReason: data.failureReason,
              });
              if (parsedMessage) {
                setMessages((prev) =>
                  parsedMessage.sender === "task_activated"
                    ? mergeSkillStatusMessage(prev, parsedMessage)
                    : appendUniqueMessage(prev, parsedMessage),
                );
              }
            }
          } catch {
            console.error("Invalid message received:", event.data);
          }
        };

        socket.onclose = (event) => {
          console.warn("[Chat] WebSocket closed.", {
            wsUrl,
            code: event.code,
            reason: event.reason,
            wasClean: event.wasClean,
          });
          // Try to reconnect after a delay if it wasn't a clean close
          if (!event.wasClean) {
            scheduleReconnect();
          }
        };

        socket.onerror = (event) => {
          console.error("[Chat] WebSocket error.", { wsUrl, event });
        };

        return socket;
      } catch (error) {
        console.error("[Chat] Error creating WebSocket connection.", {
          wsUrl,
          error,
        });
        return null;
      }
    };

    // Establish the initial connection
    const socket = connectWebSocket();

    // Cleanup function
    return () => {
      shouldReconnect = false;
      clearReconnectTimeout();
      if (socket) {
        socket.close();
      }
    };
  }, [useDirectRobot, robotWsUrl, backendWsBaseUrl]);

  useEffect(() => {
    const handleManualSkillEvent = (event: Event) => {
      const parsedMessage = parseRosbridgeChatMessage(
        (event as CustomEvent).detail,
      );
      if (!parsedMessage || parsedMessage.sender !== "task_activated") {
        return;
      }
      setMessages((prev) => mergeSkillStatusMessage(prev, parsedMessage));
    };

    window.addEventListener("manual-skill-event", handleManualSkillEvent);
    return () => {
      window.removeEventListener("manual-skill-event", handleManualSkillEvent);
    };
  }, []);

  useEffect(() => {
    if (isScrolledToBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isScrolledToBottom]);

  // Listen for voice transcription events from App.tsx
  useEffect(() => {
    const handleVoiceTranscription = (event: CustomEvent<{ text: string }>) => {
      const text = event.detail.text;
      if (
        text &&
        wsRef.current &&
        wsRef.current.readyState === WebSocket.OPEN
      ) {
        if (useDirectRobot) {
          const timestamp = Math.floor(Date.now() / 1000);
          const outgoingMessage = {
            text,
            sender: "user",
            timestamp,
          } as const;

          wsRef.current.send(
            JSON.stringify({
              op: "publish",
              topic: CHAT_IN_TOPIC,
              msg: { data: JSON.stringify(outgoingMessage) },
            }),
          );

          setMessages((prev) =>
            appendUniqueMessage(prev, {
              sender: outgoingMessage.sender,
              text: outgoingMessage.text,
              timestamp: outgoingMessage.timestamp,
            }),
          );
        } else {
          wsRef.current.send(text);
        }
        console.log("Sent voice transcription to chat:", text);
      }
    };

    window.addEventListener(
      "voice-transcription",
      handleVoiceTranscription as EventListener,
    );
    return () => {
      window.removeEventListener(
        "voice-transcription",
        handleVoiceTranscription as EventListener,
      );
    };
  }, [useDirectRobot]);

  const handleSend = async () => {
    const cleanDraft = draft.trim();
    if (!cleanDraft || !wsRef.current) return;

    // Initialize AudioContext on user interaction
    await ensureAudioContext();

    // Check if WebSocket is open
    if (wsRef.current.readyState === WebSocket.OPEN) {
      if (useDirectRobot) {
        const timestamp = Math.floor(Date.now() / 1000);
        const outgoingMessage = {
          text: cleanDraft,
          sender: "user",
          timestamp,
        } as const;

        wsRef.current.send(
          JSON.stringify({
            op: "publish",
            topic: CHAT_IN_TOPIC,
            msg: { data: JSON.stringify(outgoingMessage) },
          }),
        );

        setMessages((prev) =>
          appendUniqueMessage(prev, {
            sender: outgoingMessage.sender,
            text: outgoingMessage.text,
            timestamp: outgoingMessage.timestamp,
          }),
        );
      } else {
        // Send the draft message to the backend via WebSocket
        wsRef.current.send(cleanDraft);
      }

      // Clear the input
      setDraft("");
    } else {
      // Add a message to the UI
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "You are not connected. Attempting to reconnect...",
          timestamp: Date.now() / 1000,
        },
      ]);
    }
  };

  const stopAgent = async () => {
    try {
      if (useDirectRobot) {
        await stopAgentDirect(robotWsUrl);
      } else {
        const baseUrl =
          import.meta.env.VITE_SIM_BASE_URL ?? "http://localhost:8000";
        const response = await fetch(`${baseUrl}/stop_agent`, {
          method: "POST",
        });
        const data = await response.json();
        console.log("Agent stopped:", data);
      }

      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "Agent stopped.",
          timestamp: Date.now() / 1000,
        },
      ]);
    } catch (error) {
      console.error("Error stopping agent:", error);
      setMessages((prev) => [
        ...prev,
        {
          sender: "system",
          text: "Error stopping agent.",
          timestamp: Date.now() / 1000,
          isError: true,
        },
      ]);
    }
  };

  const filteredMessages = messages.filter(
    (msg) => msg.sender !== "vision_agent_output",
  );

  // Use the grouping utility to prepare messages for display.
  const groupedMessages: DisplayMessage[] = groupMessages(filteredMessages);

  // Initialize Cartesia client
  useEffect(() => {
    if (!cartesiaRef.current) {
      cartesiaRef.current = new CartesiaClient({
        apiKey: import.meta.env.VITE_CARTESIA_API_KEY || "",
      });
    }

    // Don't create AudioContext here automatically
    // We'll create it on first user interaction instead

    return () => {
      // Clean up WebSocket connection if active
      if (websocketRef.current) {
        websocketRef.current.disconnect();
      }

      // Stop any playing audio
      if (audioSourceRef.current) {
        try {
          audioSourceRef.current.stop();
        } catch {
          // Ignore errors if already stopped
        }
      }
    };
  }, []);

  // Function to ensure AudioContext is created and resumed
  const ensureAudioContext = async () => {
    // Create AudioContext if it doesn't exist
    if (!audioContextRef.current) {
      const AudioContextClass =
        window.AudioContext ||
        (window as Window & { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (!AudioContextClass) {
        throw new Error("AudioContext is not available in this browser.");
      }
      audioContextRef.current = new AudioContextClass();
    }

    // Resume the AudioContext if it's suspended
    if (audioContextRef.current.state === "suspended") {
      await audioContextRef.current.resume();
    }

    return audioContextRef.current.state === "running";
  };

  // Function to decode base64 to array buffer
  const base64ToArrayBuffer = (base64: string) => {
    const binaryString = atob(base64);
    const len = binaryString.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes.buffer;
  };

  // Function to speak messages using Cartesia WebSocket
  const speakMessage = async (text: string, isUser: boolean = false) => {
    if (!cartesiaRef.current || isSpeaking || !text.trim()) return;

    try {
      // Don't try to auto-initialize AudioContext for messages that arrive automatically
      // Only proceed if we already have a running AudioContext
      if (
        !audioContextRef.current ||
        audioContextRef.current.state !== "running"
      ) {
        console.log("AudioContext not running, can't autoplay speech");
        return;
      }

      setIsSpeaking(true);
      // Clear any existing audio queue
      setAudioQueue([]);
      audioBuffersRef.current = [];
      isPlayingRef.current = false;

      // Initialize WebSocket if not already done
      if (!websocketRef.current) {
        websocketRef.current = cartesiaRef.current.tts.websocket({
          container: "raw",
          encoding: "pcm_f32le",
          sampleRate: 44100,
        });

        try {
          await websocketRef.current.connect();
        } catch (error) {
          console.error(`Failed to connect to Cartesia: ${error}`);
          setIsSpeaking(false);
          return;
        }
      }

      console.log(`Speaking ${isUser ? "user" : "robot"} message: "${text}"`);

      // Create a stream - use different voices for user vs robot
      const response = await websocketRef.current.send({
        modelId: "sonic-english",
        voice: {
          mode: "id",
          // Use different voice IDs for user vs robot for easy distinction
          id: isUser
            ? "a0e99841-438c-4a64-b679-ae501e7d6091" // User voice
            : "a0e99841-438c-4a64-b679-ae501e7d6091", // Robot voice (different ID)
        },
        transcript: text,
      });

      // Process audio data
      response.on("message", (message: string) => {
        // Handle chunked audio data
        // Parse the message
        const parsedMessage = JSON.parse(message);

        if (parsedMessage.type === "error") {
          console.error("Error during speech synthesis:", parsedMessage);
        }

        if (
          parsedMessage.type === "chunk" &&
          parsedMessage.data &&
          audioContextRef.current
        ) {
          try {
            // Convert base64 string to binary data
            const audioBuffer = base64ToArrayBuffer(parsedMessage.data);

            // Convert to Float32Array for Web Audio API
            const floatArray = new Float32Array(audioBuffer);

            // Add to the queue of chunks to play
            setAudioQueue((prevQueue) => [...prevQueue, floatArray]);
          } catch (error) {
            console.error("Error processing audio chunk:", error);
          }
        }

        // Handle completion
        if (parsedMessage.type === "chunk" && parsedMessage.done) {
          console.log(
            `Finished receiving ${isUser ? "user" : "robot"} message audio`,
          );
        }
      });

      // Handle errors
      const responseWithErrorHandler = response as typeof response & {
        on(event: "error", callback: (error: unknown) => void): void;
      };
      responseWithErrorHandler.on("error", (error: unknown) => {
        console.error("Error during speech synthesis:", error);
        setIsSpeaking(false);
      });
    } catch (error) {
      console.error("Error generating speech:", error);
      setIsSpeaking(false);
    }
  };

  // Effect to monitor the audio queue and play chunks sequentially
  useEffect(() => {
    const playNextChunk = async () => {
      if (
        !audioContextRef.current ||
        audioQueue.length === 0 ||
        isPlayingRef.current
      ) {
        return;
      }

      // Mark as playing
      isPlayingRef.current = true;

      // Get the next chunk
      const nextChunk = audioQueue[0];

      try {
        // Create an audio buffer
        const audioBuffer = audioContextRef.current.createBuffer(
          1, // mono
          nextChunk.length,
          44100, // sample rate
        );

        // Fill the buffer with our audio data
        audioBuffer.getChannelData(0).set(nextChunk);

        // Create a buffer source
        const source = audioContextRef.current.createBufferSource();
        audioSourceRef.current = source;
        source.buffer = audioBuffer;

        // When playback ends
        source.onended = () => {
          // Remove the played chunk from the queue
          setAudioQueue((prevQueue) => prevQueue.slice(1));
          audioSourceRef.current = null;
          isPlayingRef.current = false;

          // If this was the last chunk, we're done speaking
          if (audioQueue.length <= 1) {
            setIsSpeaking(false);
          }
        };

        // Connect to the audio context destination and play
        source.connect(audioContextRef.current.destination);
        source.start();
      } catch (error) {
        console.error("Error playing audio chunk:", error);
        isPlayingRef.current = false;
        setAudioQueue((prevQueue) => prevQueue.slice(1));

        // If this was the last chunk, we're done speaking
        if (audioQueue.length <= 1) {
          setIsSpeaking(false);
        }
      }
    };

    // Try to play the next chunk whenever the queue changes
    playNextChunk();
  }, [audioQueue]);

  // Stop speaking function - update to clear the queue
  const stopSpeaking = () => {
    if (audioSourceRef.current) {
      try {
        audioSourceRef.current.stop();
      } catch {
        // Ignore errors if already stopped
      }
      audioSourceRef.current = null;
    }
    setAudioQueue([]); // Clear the queue
    isPlayingRef.current = false;
    setIsSpeaking(false);
  };

  // Modify the effect that handles new messages
  useEffect(() => {
    if (messages.length > 0) {
      const latestMessage = messages[messages.length - 1];

      // Don't try to auto-speak messages anymore
      // If the latest message is a robot message, speak it
      if (latestMessage.sender === "robot") {
        console.log("Speaking robot message");
        speakMessage(latestMessage.text, false);
      }

      // Scroll to bottom if needed
      if (isScrolledToBottom) {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
      }
    }
  }, [messages]);

  return (
    <ChatContainer>
      <MessagesWrapper ref={containerRef} onScroll={handleScroll}>
        {groupedMessages.map((message, index) => {
          if (message.sender === "user") {
            return (
              <InteractionMessage key={`${message.sender}-${index}`}>
                <InteractionLabel $tone="user">User</InteractionLabel>
                <InteractionBody $tone="user">{message.text}</InteractionBody>
              </InteractionMessage>
            );
          } else if (message.sender === "robot") {
            return (
              <InteractionMessage key={`${message.sender}-${index}`}>
                <InteractionLabel $tone="mars">MARS</InteractionLabel>
                <InteractionBody $tone="mars">{message.text}</InteractionBody>
              </InteractionMessage>
            );
          } else if (message.sender === "robot_grouped") {
            return (
              <RobotGroupedBubble
                key={`${message.sender}-${index}`}
                groupedExtras={message.groupedExtras}
                durationSeconds={message.durationSeconds}
                isLast={index === groupedMessages.length - 1}
              />
            );
          } else if (message.sender === "system") {
            return (
              <InteractionMessage key={`${message.sender}-${index}`}>
                <InteractionLabel $tone="system">System</InteractionLabel>
                <InteractionBody $tone="system" $isError={message.isError}>
                  {message.text}
                </InteractionBody>
              </InteractionMessage>
            );
          } else if (message.sender === "task_activated") {
            return (
              <InteractionMessage key={`${message.sender}-${index}`}>
                <InteractionLabel $tone="skill">Skill Triggered</InteractionLabel>
                <InteractionBody $tone="skill">
                  <SkillLine>
                    <SkillPulse $active={message.taskStatus === "running"} />
                    <span>{message.text}</span>
                    <SkillStatusText $status={message.taskStatus}>
                      {taskStatusLabel(message.taskStatus)}
                    </SkillStatusText>
                  </SkillLine>
                  {message.failureReason && (
                    <div style={{ marginTop: 6, opacity: 0.8 }}>
                      {message.failureReason}
                    </div>
                  )}
                </InteractionBody>
              </InteractionMessage>
            );
          }
          return null;
        })}
        {/* Marker element for auto-scroll */}
        <div ref={messagesEndRef} />
      </MessagesWrapper>

      {/* Add a stop speaking button when active */}
      {isSpeaking && (
        <div
          style={{
            position: "fixed",
            bottom: "80px",
            right: "20px",
            zIndex: 100,
          }}
        >
          <button
            onClick={stopSpeaking}
            style={{
              background: "rgba(239, 68, 68, 0.9)",
              color: "white",
              border: "none",
              borderRadius: "50%",
              width: "40px",
              height: "40px",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              boxShadow: "0 2px 10px rgba(0,0,0,0.2)",
            }}
          >
            🔇
          </button>
        </div>
      )}

      <InputArea>
        <StopButton onClick={stopAgent} title="Stop current agent action">
          <IoStop size={20} />
        </StopButton>
        <TextInput
          type="text"
          placeholder="Type your message..."
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleSend();
            }
          }}
        />
        <SendButton onClick={handleSend}>
          <IoSend size={20} style={{ display: "block", padding: 0 }} />
        </SendButton>
      </InputArea>
    </ChatContainer>
  );
}
