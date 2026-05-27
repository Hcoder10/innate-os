#!/usr/bin/env zsh
set -eo pipefail

ROS_WS="${INNATE_OS_ROS_WS:-$HOME/innate-os/ros2_ws}"
PREBUILT_ROOT="${INNATE_OS_PREBUILT_ROS_ROOT:-/opt/innate-os-prebuilt/ros2_ws}"

source /opt/ros/humble/setup.zsh
cd "$ROS_WS"

colcon_build() {
    colcon build --symlink-install \
        --cmake-args \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache \
        -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
}

clean_ros_build_artifacts() {
    mkdir -p build install log
    find build install log -mindepth 1 -maxdepth 1 -exec rm -rf {} +
}

is_stale_symlink_build_failure() {
    local output_file="$1"
    grep -q "failed to create symbolic link" "$output_file" &&
        grep -q "existing path cannot be removed: Is a directory" "$output_file"
}

colcon_build_with_retry() {
    local output_file
    output_file="$(mktemp)"

    set +e
    colcon_build 2>&1 | tee "$output_file"
    local colcon_status="${pipestatus[1]}"
    set -e

    if [[ "$colcon_status" -eq 0 ]]; then
        record_local_install_marker
        rm -f "$output_file"
        return 0
    fi

    if is_stale_symlink_build_failure "$output_file"; then
        echo "Detected stale ROS symlink-install build artifacts; cleaning build/install/log and retrying."
        clean_ros_build_artifacts
        rm -f "$output_file"
        colcon_build
        record_local_install_marker
        return
    fi

    rm -f "$output_file"
    return "$colcon_status"
}

current_source_hash() {
    find src -type f \
        ! -path '*/__pycache__/*' \
        ! -name '*.pyc' \
        -print0 | sort -z | xargs -0 -r sha256sum | sha256sum | awk '{print $1}'
}

record_local_install_marker() {
    mkdir -p install
    current_source_hash > install/.innate-local-source.sha256
}

seed_prebuilt_install() {
    [[ -f "$PREBUILT_ROOT/install/setup.zsh" ]] || return 1
    [[ -f "$PREBUILT_ROOT/source.sha256" ]] || return 1
    [[ -f "$PREBUILT_ROOT/install.sha256" ]] || return 1

    if find "$PREBUILT_ROOT/install" -xtype l -print -quit | grep -q .; then
        echo "Prebuilt ROS workspace install has dangling symlinks; running colcon build."
        return 1
    fi

    prebuilt_hash="$(cat "$PREBUILT_ROOT/source.sha256")"
    prebuilt_install_hash="$(cat "$PREBUILT_ROOT/install.sha256")"
    current_hash="$(current_source_hash)"
    if [[ "$current_hash" != "$prebuilt_hash" ]]; then
        echo "Local ROS source differs from the prebuilt image; running colcon build."
        return 1
    fi

    if [[ ! -f install/setup.zsh ]] ||
        [[ "$(cat install/.innate-prebuilt-source.sha256 2>/dev/null)" != "$prebuilt_hash" ]] ||
        [[ "$(cat install/.innate-prebuilt-install.sha256 2>/dev/null)" != "$prebuilt_install_hash" ]]; then
        echo "Using prebuilt ROS workspace install from image."
        mkdir -p install
        find install -mindepth 1 -maxdepth 1 -exec rm -rf {} +
        cp -a "$PREBUILT_ROOT/install/." install/
        echo "$prebuilt_hash" > install/.innate-prebuilt-source.sha256
        echo "$prebuilt_install_hash" > install/.innate-prebuilt-install.sha256
    else
        echo "Prebuilt ROS workspace install is current."
    fi

    echo "ROS workspace install is current; skipping rebuild."
    return 0
}

install_is_stale() {
    [[ ! -f install/setup.zsh ]] && return 0
    find install -xtype l -print -quit | grep -q . && return 0

    local installed_source_hash
    installed_source_hash="$(cat install/.innate-local-source.sha256 2>/dev/null || true)"
    [[ -z "$installed_source_hash" ]] && return 0
    [[ "$(current_source_hash)" != "$installed_source_hash" ]] && return 0

    return 1
}

if [[ "${INNATE_OS_ALWAYS_BUILD:-0}" == "1" ]]; then
    colcon_build_with_retry
elif seed_prebuilt_install; then
    :
elif install_is_stale; then
    if [[ -d install ]] && find install -xtype l -print -quit | grep -q .; then
        echo "Removing unusable ROS install before rebuild."
        clean_ros_build_artifacts
    fi
    colcon_build_with_retry
else
    echo "ROS workspace install is current; skipping rebuild."
fi
