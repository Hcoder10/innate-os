#!/bin/zsh
# Launch ROS nodes in tmux windows with 2 panes each

SESSION_NAME="ros_nodes"
ROS_WS_PATH="$INNATE_OS_ROOT/ros2_ws"
DDS_SETUP_SCRIPT="$INNATE_OS_ROOT/config/dds/setup_dds.zsh"
RUNTIME_ENV_EXPORTS=$(python3 "$INNATE_OS_ROOT/scripts/print_runtime_env.py" --shell 2>/dev/null || true)

# ROS launch commands grouped into windows (pipe-delimited for 2 panes)
ROS_COMMAND_GROUPS=(
    "ros2 launch maurice_control app.launch.py|ros2 launch maurice_bringup maurice_bringup.launch.py"
    "ros2 launch maurice_arm arm.launch.py|ros2 launch manipulation recorder.launch.py"
    "ros2 launch brain_client brain_client.launch.py|sleep 5 && ros2 service call /calibrate std_srvs/srv/Trigger && sleep 5 && ros2 launch maurice_nav mode_manager.launch.py"
    "ros2 launch manipulation behavior.launch.py|ros2 launch brain_client input_manager.launch.py"
    "ros2 launch maurice_cam camera_composable.launch.py|ros2 launch maurice_control udp_leader_receiver.launch.py"
    "ros2 launch maurice_arm ik.launch.py|cd ~/innate-os && ros2 launch innate_logger logger.launch.py"
    "cd ~/innate-os && ros2 run innate_training_node training_node|cd ~/innate-os && ros2 launch innate_uninavid uninavid.launch.py"
)

WINDOW_NAMES=(
    "app-bringup"
    "arm-recorder"
    "brain-nav"
    "behaviors-inputs"
    "cam-leader"
    "ik-logger"
    "training-uninavid"
)

# Collapse the per-pane environment setup (runtime env exports + DDS/ROS
# sourcing) into one file the panes source. This keeps the command echoed in
# each pane short and, importantly, keeps the service key off the screen and
# out of the scrollback.
#
# The file holds INNATE_SERVICE_KEY, so create it securely: mktemp makes it
# atomically at mode 0600 with an unpredictable name, closing the umask race
# (a plain `>` would briefly create it world-readable) and the symlink-attack
# window on a predictable /tmp path.
PANE_SETUP_FILE=$(mktemp "${TMPDIR:-/tmp}/innate_pane_env.XXXXXX") || {
    echo "ERROR: Failed to create pane setup tempfile." >&2
    exit 1
}
# Clean up the service-key-bearing tempfile on the error-exit paths below. The
# normal path can't rely on this trap (the script ends in `exec sleep infinity`,
# which never fires EXIT) — a backgrounded delete handles that case instead.
trap 'rm -f "$PANE_SETUP_FILE"' EXIT INT TERM
{
    echo "${RUNTIME_ENV_EXPORTS:-true}"
    echo "source $DDS_SETUP_SCRIPT"
    echo "source $ROS_WS_PATH/install/setup.zsh"
} > "$PANE_SETUP_FILE"
PANE_SETUP_CMD="source $PANE_SETUP_FILE"

echo "Launching ROS nodes in tmux session '$SESSION_NAME'..."

# Source environment
source "$DDS_SETUP_SCRIPT" || { echo "ERROR: Failed to source DDS setup." >&2; exit 1; }

if [ -f "$ROS_WS_PATH/install/setup.zsh" ]; then
    source "$ROS_WS_PATH/install/setup.zsh" || { echo "ERROR: Failed to source ROS workspace." >&2; exit 1; }
else
    echo "ERROR: ROS workspace setup not found at $ROS_WS_PATH/install/setup.zsh" >&2
    exit 1
fi

if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    tmux kill-session -t $SESSION_NAME
    sleep 1
fi

process_command_group() {
    local group_index=$1
    local command_group="${ROS_COMMAND_GROUPS[$group_index]}"
    local window_name="${WINDOW_NAMES[$group_index]}"
    local commands=(${(s:|:)command_group})
    
    echo "  Creating window: $window_name"
    
    if [ $group_index -eq 1 ]; then
        tmux new-session -d -s $SESSION_NAME -n "$window_name" -c ~ || return 1
    else
        tmux new-window -t $SESSION_NAME -n "$window_name" -c ~ || return 1
    fi
    
    sleep 0.1
    
    local first_cmd="${commands[1]}"
    local first_cmd_full="$PANE_SETUP_CMD && $first_cmd"
    tmux send-keys -t $SESSION_NAME:"$window_name".0 "$first_cmd_full" C-m || return 1
    
    if [ ${#commands[@]} -gt 1 ]; then
        local second_cmd="${commands[2]}"
        local second_cmd_full="$PANE_SETUP_CMD && $second_cmd"
        
        tmux split-window -h -c ~ -t $SESSION_NAME:"$window_name" || return 1
        sleep 0.1
        tmux send-keys -t $SESSION_NAME:"$window_name".1 "$second_cmd_full" C-m || return 1
        tmux select-layout -t $SESSION_NAME:"$window_name" even-horizontal
    fi
    
    sleep 0.1
    return 0
}

for i in $(seq 1 ${#ROS_COMMAND_GROUPS[@]}); do
    process_command_group $i || { 
        echo "ERROR: Failed to create window $i" >&2
        tmux kill-session -t $SESSION_NAME 2>/dev/null
        exit 1
    }
done

tmux select-window -t $SESSION_NAME:"${WINDOW_NAMES[1]}"

SESSION_FMT=$(printf "%-29s" "$SESSION_NAME")
WINDOWS_FMT=$(printf "%-29s" "${#WINDOW_NAMES[@]}")
ATTACH_FMT=$(printf "%-29s" "tmux attach -t $SESSION_NAME")

echo ""
echo "  ╔═════════════════════════════════════════╗     ____"
echo "  ║  All systems launched ✅                ║    [O  O]"
echo "  ║                                         ║     _||_"
echo "  ║  Session:  ${SESSION_FMT}║   |      |"
echo "  ║  Windows:  ${WINDOWS_FMT}║   |______|"
echo "  ║  Attach:   ${ATTACH_FMT}║     o  o"
echo "  ║                                         ║"
echo "  ║  Run 'innate view' to monitor nodes 👀  ║"
echo "  ╚═════════════════════════════════════════╝"
echo ""

# Play startup sound after processes initialize (backgrounded, detached from terminal)
(sleep 20 && XDG_RUNTIME_DIR=/run/user/1000 gst-play-1.0 "$INNATE_OS_ROOT/config/sounds/turnon.mp3" >/dev/null 2>&1) &
disown

# Every pane has sourced the env file by now, so drop it: the service key should
# not linger in /tmp. Done in the background because `exec sleep infinity` below
# never returns, so the EXIT trap can't remove it on the normal path.
(sleep 15 && rm -f "$PANE_SETUP_FILE") &
disown

# Keep the script alive so systemd (Type=simple) considers the service running.
# When systemctl stop is called, SIGTERM kills this sleep, then ExecStop cleans up tmux.
exec sleep infinity
