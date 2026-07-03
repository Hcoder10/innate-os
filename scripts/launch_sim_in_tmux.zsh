#!/bin/zsh

# launch-sim-in-tmux.zsh
# Launches simulation environment in organized tmux windows
# Usage: ./scripts/launch-sim-in-tmux.zsh [--detach] [--brain-websocket-uri URI] [--brain-client-version VERSION]

ATTACH=1
BRAIN_WEBSOCKET_URI=""
BRAIN_CLIENT_VERSION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --detach)
      ATTACH=0
      shift
      ;;
    --brain-websocket-uri)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --brain-websocket-uri" >&2
        exit 2
      fi
      BRAIN_WEBSOCKET_URI="$2"
      shift 2
      ;;
    --brain-websocket-uri=*)
      BRAIN_WEBSOCKET_URI="${1#*=}"
      shift
      ;;
    --brain-client-version)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --brain-client-version" >&2
        exit 2
      fi
      BRAIN_CLIENT_VERSION="$2"
      shift 2
      ;;
    --brain-client-version=*)
      BRAIN_CLIENT_VERSION="${1#*=}"
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

SESSION_NAME="${INNATE_SIM_TMUX_SESSION:-innate}"
# Use braces in tmux targets so zsh does not interpret ":foo" as a parameter modifier.
TMUX_TARGET_PREFIX="${SESSION_NAME}"
STARTUP_SETTLE_SECONDS="${INNATE_SIM_TMUX_SETTLE_SECONDS:-0}"
TMUX_CLEANUP_SETTLE_SECONDS="${INNATE_SIM_TMUX_CLEANUP_SETTLE_SECONDS:-0}"

settle_after_launch() {
  if [[ "$STARTUP_SETTLE_SECONDS" != "0" && "$STARTUP_SETTLE_SECONDS" != "0.0" ]]; then
    sleep "$STARTUP_SETTLE_SECONDS"
  fi
}

# First, ensure we have a clean tmux environment
tmux kill-session -t "$SESSION_NAME" 2>/dev/null
sleep "$TMUX_CLEANUP_SETTLE_SECONDS"

# Create a new tmux session for the local Innate runtime
tmux new-session -d -x 240 -y 72 -s "$SESSION_NAME" -n zenoh

# === Window 0: Zenoh Router ===
tmux send-keys -t "${TMUX_TARGET_PREFIX}:zenoh" "ros2 run rmw_zenoh_cpp rmw_zenohd" C-m
echo "Started Zenoh router..."
settle_after_launch

# === Window 1: Rosbridge + App ===
tmux new-window -t "$SESSION_NAME" -n rosbridge-app
tmux send-keys -t "${TMUX_TARGET_PREFIX}:rosbridge-app" "ros2 launch mars_sim_bringup sim_rosbridge.launch.py" C-m
echo "Started rosbridge..."
settle_after_launch
# Split and run app
tmux split-window -t "${TMUX_TARGET_PREFIX}:rosbridge-app" -h
tmux send-keys -t "${TMUX_TARGET_PREFIX}:rosbridge-app.1" "ros2 launch mars_control app.sim.launch.py" C-m
echo "Started app control..."

# === Window 2: WebRTC Streamer (sim) ===
# The C++ GStreamer streamer (mars_cam) routes camera video through ROS image
# topics. In the sim it is replaced by the Python aiortc server in sim/ (started
# by sim/main.py), which pulls frames directly and streams sim->browser without
# the ROS hop. Both speak the same /webrtc/* rosbridge signaling, so running both
# would conflict — leave the C++ streamer disabled here for the sim.
#
# DO NOT try to run mars_cam in the sim container instead of the host aiortc
# server. It was attempted and reverted after burning hours on docker NAT: the
# container is on the docker BRIDGE, so mars_cam's ICE host candidates carry the
# unreachable container IP, and the browser's mDNS candidates can't be reached
# back (the peer-reflexive/local-STUN srflx path collapses to the docker gateway
# and the userland-proxy hairpin fails). Workarounds all have dealbreakers:
# announce-IP + published ports only fixes browser->robot (robot still can't
# reach the browser); host networking breaks the novnc DISPLAY and is unsupported
# on Docker Desktop/Mac; requiring a getUserMedia mic grant to de-obfuscate the
# browser is the only thing that worked, but it's an unacceptable prompt. The
# ROS image path is also throttled (~10 Hz, frame-dropping) vs the host server's
# render-rate frames. The host aiortc server sits in the browser's own network
# namespace, so none of this applies. Keep WebRTC on the host.
# tmux new-window -t "$SESSION_NAME" -n webrtc
# tmux send-keys -t "${TMUX_TARGET_PREFIX}:webrtc" "ros2 launch mars_cam webrtc_streamer.sim.launch.py" C-m
echo "WebRTC: using sim/ aiortc server (C++ mars_cam streamer disabled for sim)..."

# === Window 3: Nav + Brain ===
tmux new-window -t "$SESSION_NAME" -n nav-brain
tmux send-keys -t "${TMUX_TARGET_PREFIX}:nav-brain" "ros2 launch mars_nav navigation_sim.launch.py" C-m
echo "Started navigation system..."
settle_after_launch
# Split and run brain client
tmux split-window -t "${TMUX_TARGET_PREFIX}:nav-brain" -h
brain_client_cmd="ros2 launch brain_client brain_client.sim.launch.py"
if [[ -n "$BRAIN_WEBSOCKET_URI" ]]; then
  brain_websocket_arg="websocket_uri:=$BRAIN_WEBSOCKET_URI"
  brain_client_cmd+=" ${(q)brain_websocket_arg}"
fi
if [[ -n "$BRAIN_CLIENT_VERSION" ]]; then
  brain_client_version_arg="client_version:=$BRAIN_CLIENT_VERSION"
  brain_client_cmd+=" ${(q)brain_client_version_arg}"
fi
tmux send-keys -t "${TMUX_TARGET_PREFIX}:nav-brain.1" "$brain_client_cmd" C-m
echo "Started brain client..."

# === Window 4: Behavior Server ===
tmux new-window -t "$SESSION_NAME" -n behavior
tmux send-keys -t "${TMUX_TARGET_PREFIX}:behavior" "ros2 launch manipulation behavior.launch.py" C-m
echo "Started behavior server..."

# === Window 5: Arm IK ===
tmux new-window -t "$SESSION_NAME" -n arm-ik
tmux send-keys -t "${TMUX_TARGET_PREFIX}:arm-ik" "ros2 run mars_arm ik.py" C-m
echo "Started arm IK..."

# === Window 6: Vision Navigation Inference Client ===
tmux new-window -t "$SESSION_NAME" -n vision-nav
tmux send-keys -t "${TMUX_TARGET_PREFIX}:vision-nav" "ros2 launch innate_uninavid uninavid.launch.py cmd_vel_topic:=/cmd_vel" C-m
echo "Started vision navigation inference client..."
settle_after_launch

# === Window 7: Console + Webapp UI ===
# The robot webapp serves the sim UI too (replacing sim/frontend). Its python
# front door binds 443 (https) + 80 (http) inside the container — both exposed by
# docker-compose.dev.yml — and proxies /ws to the sim rosbridge on 9090.
tmux new-window -t "$SESSION_NAME" -n console-webapp
tmux send-keys -t "${TMUX_TARGET_PREFIX}:console-webapp" "ros2 launch innate_console console.launch.py" C-m
echo "Started console..."
settle_after_launch
tmux split-window -t "${TMUX_TARGET_PREFIX}:console-webapp" -h
# WEBAPP_SIM_CONTROLS surfaces the webapp's sim-only debug controls (Reset
# Position + FPS/queue), which the robot deployment leaves off.
tmux send-keys -t "${TMUX_TARGET_PREFIX}:console-webapp.1" "cd ~/innate-os/webapp && while true; do WEBAPP_SIM_CONTROLS=1 python3 proxy/https_server.py; sleep 2; done" C-m
echo "Started webapp (https :443 + http :80)..."

# Select the rosbridge-app window
tmux select-window -t "${TMUX_TARGET_PREFIX}:rosbridge-app"

if [[ $ATTACH -eq 1 ]]; then
  echo "All services started in tmux session '$SESSION_NAME'. Attaching to session..."
  sleep 1
  tmux attach-session -t "$SESSION_NAME"
else
  echo "All services started in tmux session '$SESSION_NAME'."
  echo "Attach with: tmux attach-session -t $SESSION_NAME"
fi
