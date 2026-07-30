// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
/**
 * AppControl node for MARS robot.
 * Handles joystick input, leader arm control, and robot info publishing.
 */

#include <rclcpp/rclcpp.hpp>
#include <geometry_msgs/msg/vector3.hpp>
#include <geometry_msgs/msg/twist.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <brain_messages/msg/recorder_status.hpp>
#include <std_msgs/msg/int32_multi_array.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/string.hpp>
#include <mars_msgs/srv/set_robot_name.hpp>
#include <mars_msgs/srv/trigger_update.hpp>
#include <mars_msgs/srv/shutdown.hpp>
#include <mars_msgs/srv/set_volume.hpp>
#include <std_msgs/msg/bool.hpp>
#include <std_srvs/srv/set_bool.hpp>

#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <cmath>
#include <array>
#include <chrono>
#include <cstdlib>
#include <memory>
#include <regex>
#include <sstream>
#include <signal.h>

using json = nlohmann::json;
using namespace std::chrono_literals;

namespace mars_control {

// Joystick teleop tuning (mobile-app drive joystick -> /cmd_vel).
namespace joy_tuning {
// Defaults match the original pre-tuning feel: 15% deadband, quadratic
// response, 0.4 m/s / 1.0 rad/s caps, no speed-dependent turn bonus.
constexpr double DEADZONE = 0.15;
constexpr double EXPONENT = 2.0;     // quadratic; 1.0 = linear
constexpr double MAX_LINEAR = 0.4;   // m/s
constexpr double MAX_ANGULAR = 1.0;  // rad/s
// Car-like steering inversion while reversing is the `reverse_steering` ROS
// parameter (default off); the app's Dev tab toggles it live via set_parameters.
constexpr double REVERSE_STEER_RAMP = 0.15;  // m/s

// Drive smoothing. /joystick only sets a target; a fixed-rate timer ramps the published
// twist toward it, so the motors never see a step. Same lag-plus-slew shape as
// JoystickAxis::update in joystick.py, with deceleration and jerk limited separately.
constexpr double CONTROL_DT = 0.02;  // s, smoother tick == 50 Hz; matches motion_control.yaml
// Per axis, so the angular ramp can be tightened without changing linear feel.
constexpr double SPEED_TIME_CONSTANT = 0.40;          // s, first-order lag, linear
constexpr double ANGULAR_SPEED_TIME_CONSTANT = 0.10;  // s, first-order lag, angular
constexpr double MAX_ACCELERATION = 0.2;              // m/s^2
constexpr double MAX_DECELERATION = 1.2;              // m/s^2, larger so stopping stays responsive
constexpr double MAX_ANGULAR_ACCELERATION = 2.0;      // rad/s^2
// Angular decel and jerk run much harder than the linear pair: a slow yaw ramp-down keeps
// turning after the operator has stopped asking, which reads as the robot refusing to
// straighten. At 3.0 / 25.0 releasing a full-rate turn swept 12 deg; these sweep ~7.
constexpr double MAX_ANGULAR_DECELERATION = 6.0;  // rad/s^2
// How fast the accel limits themselves may change. Without this the accel -> decel
// handover is a step and the chassis is kicked into the stop.
// Keep time_constant >= max_deceleration / (2 * max_jerk) on both axes, or the axis
// arrives at rest still braking and that torque is cut in one tick.
constexpr double MAX_JERK = 10.0;           // m/s^3
constexpr double MAX_ANGULAR_JERK = 100.0;  // rad/s^3
// Under the mux's 0.5 s teleop window, above the app's 150 ms heartbeat, so a dropped
// link ramps down under our own decel limit instead of being cut to zero by the mux.
constexpr double INPUT_TIMEOUT = 0.4;  // s
// Snap to the target below this. A hardware floor: bringup sends int(v * 100), so 0.01
// is one wire count and anything finer is motion the motors cannot express.
constexpr double SETTLE_EPSILON = 0.01;  // m/s and rad/s, == 1 wire count

// Speed policy: some robot activities want a different default speed than whatever the
// operator last picked. Mapping wants care; episode recording wants repeatability.
//
// Applied on *entry* only, never enforced while there -- if the operator deliberately picks
// another mode mid-map, yanking them back would be worse than not helping. On exit the
// previous value is restored, but only if it is still the one we set: if the operator (or
// another client -- speed_scale is global) changed it since, that choice is theirs to keep.
constexpr const char* MAPPING_MODE_SCALE_ID = "slow";
constexpr const char* RECORDING_SCALE_ID = "medium";

// Heading hold. Closes the yaw loop that nothing else closes: the drive wheels track their
// RPM setpoints while the robot still yaws, because the caster applies an external torque
// that wheel-level control cannot see. Measured at 28 deg/m driving forward, ~10x less in
// reverse -- the asymmetry of a caster that leads one way and trails the other.
//
// Proportional only. It does not null the disturbance, it bounds it: unbounded drift
// becomes a small standing offset (drift / gain), with no integrator to wind up. Feedback
// is /odom's IMU heading, quantised to 0.01 rad by the I2C link, which is fine for a term
// that integrates and hopeless for one that differentiates -- hence no D.
constexpr double HEADING_GAIN = 5.0;            // rad/s of correction per rad of error
constexpr double HEADING_MAX_CORRECTION = 1.0;  // rad/s, the full manual turn rate
// Seconds of memory. The target bleeds toward the measured heading at this rate, so the
// loop resists being turned now but does not restore heading lost earlier -- the operator
// corrects their own overshoot. Residual drift is roughly disturbance / (1 + gain * leak),
// so longer memory rejects better but takes longer to forget. 0 disables the bleed and
// makes this an absolute heading lock.
constexpr double HEADING_LEAK = 0.2;              // s
constexpr double HEADING_DEADBAND = 0.01;         // rad, one odom quantum; stops chattering
constexpr double HEADING_SLEW = 2.0;              // rad/s^2, so engaging is not a step
constexpr double HEADING_STRAIGHT_YAW = 0.05;     // rad/s, below this we count as straight
constexpr double HEADING_MIN_SPEED = 0.01;        // m/s, above this we are actually moving
constexpr double HEADING_FEEDBACK_TIMEOUT = 0.3;  // s, stale /odom disengages the hold
// Manual-drive speed mode: multiplies the motion_control caps, so it can lower
// the configured maximum but never exceed it. The app/webapp set it live.
constexpr double SPEED_SCALE = 1.0;
// The presets the pickers offer, published on /robot/info so both clients render the same
// set without each carrying its own copy.
struct SpeedMode {
    const char* id;
    const char* label;
    double scale;
};
constexpr std::array<SpeedMode, 4> SPEED_MODES{
    {{"slow", "Slow", 0.35}, {"medium", "Med", 0.65}, {"fast", "Fast", 1.0}, {"mad", "Mad", 2.0}}};

/**
 * The largest scale the robot itself offers.
 *
 * The clamp below uses this rather than a literal 1.0, which is what keeps the safety
 * property intact now that a mode exceeds the configured caps: a client picks among modes
 * the robot published and cannot invent one. Adding a faster mode is a robot-side change,
 * and bringup's `safety` clamp is still the hard ceiling at the motors regardless.
 */
/** Scale of a preset by id, or 0 if there is no such mode. */
inline double scale_for_mode(const char* id) {
    for (const auto& mode : SPEED_MODES) {
        if (std::string(mode.id) == id) {
            return mode.scale;
        }
    }
    return 0.0;
}

constexpr double max_speed_scale() {
    double largest = 0.0;
    for (const auto& mode : SPEED_MODES) {
        if (mode.scale > largest) {
            largest = mode.scale;
        }
    }
    return largest;
}
// None of the knobs above means "unlimited" at zero -- see smoothing_param(), which
// substitutes these same defaults for a non-positive override.
}  // namespace joy_tuning

/**
 * Check if running inside a Docker container.
 * Returns true if inside Docker, false otherwise.
 */
bool is_running_in_docker() {
    // Check for /.dockerenv file (most reliable)
    if (std::filesystem::exists("/.dockerenv")) {
        return true;
    }
    // Fallback: check cgroup for docker/container indicators
    std::ifstream cgroup("/proc/1/cgroup");
    if (cgroup.is_open()) {
        std::string line;
        while (std::getline(cgroup, line)) {
            if (line.find("docker") != std::string::npos || line.find("kubepods") != std::string::npos ||
                line.find("containerd") != std::string::npos) {
                return true;
            }
        }
    }
    return false;
}

/**
 * Execute a shell command and return its output (trimmed).
 */
std::string exec_command(const std::string& cmd) {
    std::array<char, 256> buffer;
    std::string result;
    std::unique_ptr<FILE, decltype(&pclose)> pipe(popen(cmd.c_str(), "r"), pclose);
    if (!pipe) {
        return "";
    }
    while (fgets(buffer.data(), buffer.size(), pipe.get()) != nullptr) {
        result += buffer.data();
    }
    // Trim trailing newline
    while (!result.empty() && (result.back() == '\n' || result.back() == '\r')) {
        result.pop_back();
    }
    return result;
}

/**
 * Get the Bluetooth device ID (MAC address) of the first available adapter.
 * Returns the MAC address as a string, or empty string if not available.
 */
std::string get_bluetooth_device_id() {
    try {
        // Use bluetoothctl to get the default adapter address
        std::string result =
            exec_command("bluetoothctl show 2>/dev/null | grep 'Controller' | head -1 | awk '{print $2}'");

        // Validate MAC address format
        std::regex mac_regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$");
        if (std::regex_match(result, mac_regex)) {
            return result;
        }
        return "";
    } catch (...) {
        return "";
    }
}

/**
 * Get the system hostname.
 * Returns the hostname as a string, or empty string if not available.
 * In Docker, skips avahi-resolve (not available) and uses simple hostname.
 */
std::string get_hostname() {
    std::string result;

    // Skip avahi-resolve in Docker (not available)
    if (!is_running_in_docker()) {
        result = exec_command("avahi-resolve -a $(hostname -I | awk '{print $1}') | awk '{print $2}' 2>/dev/null");
    }

    if (result.empty()) {
        // Fallback to hostname command
        result = exec_command("hostname 2>/dev/null") + ".local";
    }
    // Ensure hostname ends with .local
    const std::string suffix = ".local";
    if (result.length() < suffix.length() ||
        result.compare(result.length() - suffix.length(), suffix.length(), suffix) != 0) {
        result += suffix;
    }
    return result;
}

static const std::regex RELEASE_VERSION_TAG_REGEX(R"(^(\d+)\.(\d+)\.(\d+)(-rc\d+)?$)");

static bool is_release_version_tag(const std::string& tag) {
    return std::regex_match(tag, RELEASE_VERSION_TAG_REGEX);
}

/**
 * Get the latest OS release tag (x.y.z or x.y.z-rcN). Skips unrelated tags
 * such as sim-assets-<sha> used for simulator asset bundles.
 */
std::string get_latest_release_tag(const std::string& mars_root) {
    std::string tags_cmd = "cd \"" + mars_root + "\" && git tag --list --sort=-version:refname 2>/dev/null";
    std::string tags_output = exec_command(tags_cmd);
    if (tags_output.empty()) {
        return "";
    }

    std::istringstream iss(tags_output);
    std::string tag;
    while (std::getline(iss, tag)) {
        if (is_release_version_tag(tag)) {
            return tag;
        }
    }
    return "";
}

/**
 * Get the latest tag by version. Returns empty string if the repo has no tags.
 */
std::string get_latest_tag(const std::string& mars_root) {
    return get_latest_release_tag(mars_root);
}

/**
 * Get the subject (first line) of the latest tag's annotation message.
 * Returns empty string if no tags or tag has no message.
 */
std::string get_tag_subject(const std::string& mars_root) {
    std::string latest_tag = get_latest_tag(mars_root);
    if (latest_tag.empty()) {
        return "";
    }

    // Get the tag message subject (first line only)
    std::string subject_cmd =
        "cd \"" + mars_root + "\" && git tag -l --format='%(contents:subject)' \"" + latest_tag + "\" 2>/dev/null";
    return exec_command(subject_cmd);
}

/**
 * Get the body (everything after first line) of the latest tag's annotation message.
 * Returns empty string if no tags or tag has no body.
 */
std::string get_tag_body(const std::string& mars_root) {
    std::string latest_tag = get_latest_tag(mars_root);
    if (latest_tag.empty()) {
        return "";
    }

    // Get the tag message body (everything after subject line)
    std::string body_cmd =
        "cd \"" + mars_root + "\" && git tag -l --format='%(contents:body)' \"" + latest_tag + "\" 2>/dev/null";
    return exec_command(body_cmd);
}

/**
 * Get the current robot version.
 * - If HEAD is exactly on a tag (branch or detached), returns that tag
 * - Otherwise (development or detached HEAD), returns latest tag + "-dev"
 * - If no tags exist / not a git checkout (e.g. the sim container mounts the
 *   source without .git), returns "0.0.0-dev". A real robot is always checked
 *   out on a tag, so this fallback only applies in development.
 */
std::string get_robot_version(const std::string& mars_root) {
    // Check if we're exactly on a tag (works for both branch and detached HEAD)
    std::string exact_cmd = "cd \"" + mars_root + "\" && git describe --exact-match --tags HEAD 2>/dev/null";
    std::string exact_tag = exec_command(exact_cmd);

    // If on a release tag (detached HEAD from tag checkout), return that tag.
    // Non-release tags (e.g. sim-assets-*) are ignored for OS version reporting.
    if (!exact_tag.empty() && is_release_version_tag(exact_tag)) {
        return exact_tag;
    }

    // Detached HEAD not exactly on a release tag (e.g. interrupted update, manual
    // checkout, or a feature branch) is treated like development: fall back to
    // the latest release tag + "-dev" instead of failing the version check.

    std::string latest_tag = get_latest_release_tag(mars_root);

    // No release tags, or not a git checkout at all (the sim container mounts the
    // source without .git, so git errors and 2>/dev/null leaves this empty). Report a
    // development version instead of failing every publish cycle. A real robot
    // is always on a tag, so this only happens in dev/sim.
    if (latest_tag.empty()) {
        return "0.0.0-dev";
    }

    return latest_tag + "-dev";
}

/**
 * Get active WiFi SSID using nmcli.
 */
std::string nmcli_get_active_wifi_ssid() {
    std::string result = exec_command("nmcli -t -f active,ssid dev wifi 2>/dev/null | grep '^yes:' | cut -d: -f2");
    return result;
}

/**
 * Sanitize hostname to follow DNS rules:
 * - Only letters (a-z), numbers (0-9), and hyphens (-)
 * - Cannot start or end with hyphens
 * - Max 63 characters
 */
std::string sanitize_hostname(const std::string& hostname) {
    if (hostname.empty())
        return "mars";

    std::string result;
    result.reserve(hostname.length());

    // Convert to lowercase and replace invalid chars with hyphens
    for (char c : hostname) {
        if (std::isalnum(c)) {
            result += std::tolower(c);
        } else {
            result += '-';
        }
    }

    // Remove consecutive hyphens
    result.erase(std::unique(result.begin(), result.end(), [](char a, char b) { return a == '-' && b == '-'; }),
                 result.end());

    // Trim leading/trailing hyphens
    size_t start = result.find_first_not_of('-');
    size_t end = result.find_last_not_of('-');
    if (start == std::string::npos)
        return "mars";

    result = result.substr(start, std::min(end - start + 1, size_t(63)));
    if (result.back() == '-')
        result.pop_back();

    return result.empty() ? "mars" : result;
}

/**
 * Get current system hostname using hostnamectl command.
 * Returns the current hostname as a string.
 * In Docker, returns empty string (hostnamectl requires systemd).
 */
std::string get_system_hostname() {
    if (is_running_in_docker()) {
        return "";
    }
    return exec_command("sudo hostnamectl hostname --static 2>/dev/null");
}

/**
 * Set system hostname using hostnamectl command.
 * Sets the static hostname using the hostnamectl command-line tool.
 * Uses sudo to run with elevated privileges.
 * Returns true on success, false on failure.
 * In Docker, this is a no-op (hostnamectl requires systemd).
 */
bool set_system_hostname(const std::string& sanitized_hostname, std::string& error_msg) {
    // Skip in Docker - hostnamectl requires systemd which isn't available
    if (is_running_in_docker()) {
        return true;  // Silently succeed - hostname sync not applicable in containers
    }

    // Use sudo hostnamectl to set the hostname
    std::string cmd = "sudo hostnamectl hostname --static \"" + sanitized_hostname + "\" 2>&1";

    // Execute the command
    int exit_code = std::system(cmd.c_str());
    if (WEXITSTATUS(exit_code) != 0) {
        error_msg = "Failed to set hostname via hostnamectl";
        return false;
    }

    // Restart avahi-daemon to apply hostname changes to mDNS
    std::system("sudo systemctl restart avahi-daemon.service 2>/dev/null");

    return true;
}

/**
 * Check if system updates are available using innate-update --quick-check.
 * Returns true if updates are available, false otherwise.
 */
bool check_update_available(const std::string& mars_root) {
    // innate-update --quick-check returns 0 if up-to-date, 1 if updates available
    std::string cmd = mars_root + "/scripts/update/innate-update quick-check >/dev/null 2>&1";
    int result = std::system(cmd.c_str());
    return (WEXITSTATUS(result) != 0);
}

/**
 * Check if a system update is currently running by checking the lock file.
 * Returns true if update is in progress (lock exists and PID is alive).
 */
bool check_update_running() {
    const std::string lock_path = "/tmp/innate-update.lock";
    std::ifstream lock_file(lock_path);
    if (!lock_file.is_open()) {
        return false;
    }

    std::string pid_str;
    if (!std::getline(lock_file, pid_str)) {
        return true;  // Malformed lock, assume running to be safe
    }

    try {
        pid_t pid = std::stoi(pid_str);
        // Check if process is alive (kill with signal 0 just checks existence)
        if (kill(pid, 0) == 0) {
            return true;  // Process exists
        }
        // Process doesn't exist - stale lock
        std::remove(lock_path.c_str());
        return false;
    } catch (...) {
        return true;  // Malformed, assume running
    }
}

class AppControl : public rclcpp::Node {
   public:
    AppControl() : Node("app_control_node") {
        // Load app configuration
        _load_app_config();

        // Cache for WiFi SSID to avoid frequent subprocess calls
        _cached_wifi_ssid = "";
        _last_wifi_check_time = 0.0;
        _wifi_check_interval = 10.0;  // Check WiFi every 10 seconds instead of every 1 second

        // Cache for update check to avoid frequent subprocess calls
        _cached_update_available = false;
        _last_update_check_time = 0.0;
        _update_check_interval = 300.0;  // Check for updates every 5 minutes

        // Declare parameters
        std::string mars_root = get_mars_root();
        this->declare_parameter("data_directory", mars_root + "/data");
        this->declare_parameter("robot_name", "");
        this->declare_parameter("default_hardware_revision", "R6");
        // Joystick: invert steering while reversing (car-like). Off by default;
        // the app's Dev tab toggles it at runtime through set_parameters.
        this->declare_parameter("reverse_steering", false);
        // Drive-speed caps; settings.yaml's /** motion_control overrides these, else joy_tuning defaults.
        this->declare_parameter("motion_control.max_speed", joy_tuning::MAX_LINEAR);
        this->declare_parameter("motion_control.max_angular_speed", joy_tuning::MAX_ANGULAR);
        // Speed mode (slow / medium / fast); the app and webapp write it live via set_parameters.
        this->declare_parameter("motion_control.speed_scale", joy_tuning::SPEED_SCALE);
        // Teleop ramp tuning. Only this node declares motion_control.*, so a settings.yaml
        // `/**` override reaches the app joystick without touching the USB gamepad node.
        this->declare_parameter("motion_control.dt", joy_tuning::CONTROL_DT);
        this->declare_parameter("motion_control.speed_time_constant", joy_tuning::SPEED_TIME_CONSTANT);
        this->declare_parameter("motion_control.angular_speed_time_constant", joy_tuning::ANGULAR_SPEED_TIME_CONSTANT);
        this->declare_parameter("motion_control.max_acceleration", joy_tuning::MAX_ACCELERATION);
        this->declare_parameter("motion_control.max_deceleration", joy_tuning::MAX_DECELERATION);
        this->declare_parameter("motion_control.max_angular_acceleration", joy_tuning::MAX_ANGULAR_ACCELERATION);
        this->declare_parameter("motion_control.max_angular_deceleration", joy_tuning::MAX_ANGULAR_DECELERATION);
        this->declare_parameter("motion_control.max_jerk", joy_tuning::MAX_JERK);
        this->declare_parameter("motion_control.max_angular_jerk", joy_tuning::MAX_ANGULAR_JERK);
        this->declare_parameter("motion_control.settle_epsilon", joy_tuning::SETTLE_EPSILON);
        this->declare_parameter("motion_control.input_timeout", joy_tuning::INPUT_TIMEOUT);
        // Heading hold. Gain 0 disables it, which is why these are not read through
        // smoothing_param -- zero is a legitimate value here, not a broken one.
        this->declare_parameter("heading_hold.gain", joy_tuning::HEADING_GAIN);
        this->declare_parameter("heading_hold.max_correction", joy_tuning::HEADING_MAX_CORRECTION);
        this->declare_parameter("heading_hold.leak", joy_tuning::HEADING_LEAK);
        this->declare_parameter("heading_hold.min_speed", joy_tuning::HEADING_MIN_SPEED);
        this->declare_parameter("heading_hold.straight_yaw", joy_tuning::HEADING_STRAIGHT_YAW);
        this->declare_parameter("heading_hold.deadband", joy_tuning::HEADING_DEADBAND);
        this->declare_parameter("heading_hold.slew", joy_tuning::HEADING_SLEW);

        // Log if running in Docker (system hostname operations will be skipped)
        if (is_running_in_docker()) {
            RCLCPP_INFO(this->get_logger(), "Running in Docker - system hostname operations disabled");
        }

        // Subscribe to joystick messages (Vector3)
        joystick_sub_ = this->create_subscription<geometry_msgs::msg::Vector3>(
            "/joystick", 10, std::bind(&AppControl::joystick_callback, this, std::placeholders::_1));

        // Speed policy inputs. Both are robot state, so the policy lives here rather than in
        // each client: it then holds for the webapp and the mobile app alike, and survives a
        // client disconnecting part-way through a map.
        nav_mode_sub_ = this->create_subscription<std_msgs::msg::String>(
            "/nav/current_mode", rclcpp::QoS(10).transient_local(), [this](const std_msgs::msg::String::SharedPtr msg) {
                nav_mode_ = msg->data;
                apply_speed_policy();
            });
        recorder_sub_ = this->create_subscription<brain_messages::msg::RecorderStatus>(
            "/brain/recorder/status", 10, [this](const brain_messages::msg::RecorderStatus::SharedPtr msg) {
                // "active" alone only means the recorder node is up; an episode is in progress
                // only once it also names one. Same test as profile_recorder_node.py.
                recording_ = (msg->status == "active" && !msg->episode_number.empty());
                apply_speed_policy();
            });

        // Heading feedback. /odom's x,y are dead-reckoned from commanded speeds and are
        // useless as feedback, but its orientation is genuine IMU heading from the MCU.
        odom_sub_ = this->create_subscription<nav_msgs::msg::Odometry>(
            "/odom", rclcpp::SensorDataQoS(), std::bind(&AppControl::odom_callback, this, std::placeholders::_1));

        // Subscribe to leader positions messages (Int32MultiArray)
        leader_sub_ = this->create_subscription<std_msgs::msg::Int32MultiArray>(
            "/leader_positions", 10, std::bind(&AppControl::leader_positions_callback, this, std::placeholders::_1));

        // Teleop input of the cmd_vel priority mux (mars_nav cmd_vel_mux.py)
        cmd_vel_pub_ = this->create_publisher<geometry_msgs::msg::Twist>("/cmd_vel_teleop", 10);

        // Publisher for leader arm commands (Float64MultiArray) on /mars/arm/commands
        cmd_pub_ = this->create_publisher<std_msgs::msg::Float64MultiArray>("/mars/arm/commands", 10);

        // Publisher for robot info
        robot_info_pub_ = this->create_publisher<std_msgs::msg::String>("/robot/info", 10);

        // Fixed-rate drive smoother, on its own timer rather than in joystick_callback:
        // /joystick arrives irregularly, so integrating on message arrival would tie the ramp
        // rate to how fast the operator moves the stick. The period is fixed here, which makes
        // motion_control.rate a startup knob; the rest are live-tunable.
        smoothing_dt_ = smoothing_param("motion_control.dt", joy_tuning::CONTROL_DT);
        drive_smoother_timer_ = this->create_wall_timer(
            std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::duration<double>(smoothing_dt_)),
            std::bind(&AppControl::drive_smoother_callback, this));

        // Timer for publishing robot info
        robot_info_timer_ = this->create_wall_timer(1000ms, std::bind(&AppControl::publish_robot_info_callback, this));

        // Timer for syncing system hostname to robot name
        hostname_sync_timer_ = this->create_wall_timer(30000ms, std::bind(&AppControl::sync_hostname_callback, this));

        // Service for setting robot name
        set_robot_name_srv_ = this->create_service<mars_msgs::srv::SetRobotName>(
            "/set_robot_name",
            std::bind(&AppControl::set_robot_name_callback, this, std::placeholders::_1, std::placeholders::_2));

        // Service for triggering system update
        trigger_update_srv_ = this->create_service<mars_msgs::srv::TriggerUpdate>(
            "/trigger_update",
            std::bind(&AppControl::trigger_update_callback, this, std::placeholders::_1, std::placeholders::_2));

        // Service for system shutdown
        shutdown_srv_ = this->create_service<mars_msgs::srv::Shutdown>(
            "/shutdown", std::bind(&AppControl::shutdown_callback, this, std::placeholders::_1, std::placeholders::_2));

        // Service for setting system volume
        set_volume_srv_ = this->create_service<mars_msgs::srv::SetVolume>(
            "/set_volume",
            std::bind(&AppControl::set_volume_callback, this, std::placeholders::_1, std::placeholders::_2));

        // Service for enabling/disabling the robot microphone
        set_microphone_srv_ = this->create_service<std_srvs::srv::SetBool>(
            "/set_microphone",
            std::bind(&AppControl::set_microphone_callback, this, std::placeholders::_1, std::placeholders::_2));

        // Latched publisher for microphone state
        mic_enabled_pub_ =
            this->create_publisher<std_msgs::msg::Bool>("/microphone_enabled", rclcpp::QoS(1).transient_local());

        // Sync mic state and ALSA volume from robot_info.json on startup
        try {
            json robot_info = get_robot_info();
            publish_mic_enabled(robot_info.value("microphone_enabled", true));
            int startup_vol = robot_info.value("volume_percent", 80);
            apply_alsa_volume(startup_vol);
            RCLCPP_INFO(this->get_logger(), "ALSA volume synced to %d%%", startup_vol);
        } catch (const std::exception& e) {
            RCLCPP_WARN(this->get_logger(), "Failed to sync startup volume/mic state: %s", e.what());
        }

        RCLCPP_INFO(this->get_logger(), "AppControl node started. [C++]");
    }

   private:
    std::string get_mars_root() {
        const char* env_root = std::getenv("INNATE_OS_ROOT");
        if (env_root) {
            return std::string(env_root);
        }
        const char* home = std::getenv("HOME");
        return std::string(home ? home : "/home/jetson1") + "/innate-os";
    }

    /**
     * Load app configuration from YAML config file.
     */
    void _load_app_config() {
        std::string mars_root = get_mars_root();
        std::string config_file_path = mars_root + "/config/os_config.yaml";

        try {
            if (std::filesystem::exists(config_file_path)) {
                YAML::Node config = YAML::LoadFile(config_file_path);
                if (config["minimum_app_version"]) {
                    app_config_["minimum_app_version"] = config["minimum_app_version"].as<std::string>();
                }
            }
        } catch (const YAML::Exception& e) {
            RCLCPP_WARN(get_logger(), "Failed to load os_config.yaml: %s", e.what());
            app_config_ = json::object();
        }
    }

    /**
     * Get the full path to robot_info.json file.
     * Returns the absolute path with ~ expansion if needed.
     */
    std::string get_robot_info_path() {
        std::string data_directory_param = this->get_parameter("data_directory").as_string();
        std::string data_dir = data_directory_param;

        // Expand ~ if present
        if (!data_dir.empty() && data_dir[0] == '~') {
            const char* home = std::getenv("HOME");
            if (home) {
                data_dir = std::string(home) + data_dir.substr(1);
            }
        }

        return data_dir + "/robot_info.json";
    }

    /**
     * Get robot_info.json contents, creating file with defaults if it doesn't exist.
     * Ensures all default keys exist in the file.
     * Returns the parsed JSON object.
     */
    json get_robot_info() {
        std::string robot_info_file_path = get_robot_info_path();

        // Ensure data directory exists
        std::filesystem::path file_path(robot_info_file_path);
        std::filesystem::create_directories(file_path.parent_path());

        // Define default robot info values
        std::string default_hw_rev = this->get_parameter("default_hardware_revision").as_string();
        json default_robot_info = {
            {"robot_name", "MARS"},     {"robot_id", nullptr},  {"hardware_revision", default_hw_rev},
            {"color_variant", "black"}, {"volume_percent", 80}, {"microphone_enabled", true}};

        json robot_info;

        // If robot_info.json does not exist, create it with default values
        if (!std::filesystem::exists(robot_info_file_path)) {
            std::ofstream out_file(robot_info_file_path);
            out_file << default_robot_info.dump(2);
            out_file.close();
            return default_robot_info;
        }

        // Read robot_info from file
        try {
            std::ifstream in_file(robot_info_file_path);
            if (!in_file.is_open()) {
                return default_robot_info;
            }
            in_file >> robot_info;
            in_file.close();
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to read robot_info.json: %s", e.what());
            return default_robot_info;
        }

        // Ensure all default keys exist in the JSON, save if any were missing
        bool updated = false;
        for (auto& [key, default_value] : default_robot_info.items()) {
            if (!robot_info.contains(key)) {
                robot_info[key] = default_value;
                updated = true;
            }
        }
        if (updated) {
            std::ofstream out_file(robot_info_file_path);
            out_file << robot_info.dump(2);
            out_file.close();
        }

        return robot_info;
    }

    /**
     * Save robot_info JSON object to robot_info.json file.
     * Returns true on success, false on failure.
     */
    bool set_robot_info(const json& robot_info) {
        try {
            std::string robot_info_file_path = get_robot_info_path();
            std::ofstream out_file(robot_info_file_path);
            if (!out_file.is_open()) {
                return false;
            }
            out_file << robot_info.dump(2);
            out_file.close();
            return true;
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to write robot_info.json: %s", e.what());
            return false;
        }
    }

    /**
     * Get WiFi SSID with caching to avoid frequent subprocess calls.
     * Only checks for new SSID if the configured interval has passed.
     */
    std::string get_cached_wifi_ssid() {
        double current_time =
            std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();

        // If we haven't checked recently or don't have a cached value, check now
        if (_cached_wifi_ssid.empty() || (current_time - _last_wifi_check_time) > _wifi_check_interval) {
            std::string new_ssid = nmcli_get_active_wifi_ssid();

            // Only log if the SSID actually changed or this is the first check
            if (new_ssid != _cached_wifi_ssid) {
                if (!new_ssid.empty()) {
                    RCLCPP_INFO(this->get_logger(), "WiFi SSID updated: %s -> %s", _cached_wifi_ssid.c_str(),
                                new_ssid.c_str());
                } else {
                    RCLCPP_WARN(this->get_logger(), "Could not retrieve WiFi SSID");
                }

                _cached_wifi_ssid = new_ssid;
            }
            _last_wifi_check_time = current_time;
        }

        return _cached_wifi_ssid;
    }

    /**
     * Check for system updates with caching to avoid frequent subprocess calls.
     * Only checks if the configured interval has passed.
     * Uses innate-update --quick-check which itself uses a 1-hour cache.
     */
    bool get_cached_update_available() {
        double current_time =
            std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();

        // Check if we need to refresh the cache
        if (_last_update_check_time == 0.0 || (current_time - _last_update_check_time) > _update_check_interval) {
            bool new_update_available = check_update_available(get_mars_root());

            // Only log if the status changed
            if (new_update_available != _cached_update_available) {
                if (new_update_available) {
                    RCLCPP_INFO(this->get_logger(), "System updates are available");
                } else {
                    RCLCPP_INFO(this->get_logger(), "System is up to date");
                }
            }

            _cached_update_available = new_update_available;
            _last_update_check_time = current_time;
        }

        return _cached_update_available;
    }

    /** Apply deadband and a power-law (joy_tuning::EXPONENT) response curve to a [-1, 1] axis. */
    double apply_curve(double value, double deadband = joy_tuning::DEADZONE) {
        // /joystick is an open topic and the curve amplifies, so an out-of-range axis would
        // scale straight past motion_control.max_speed. Bound it before shaping.
        value = std::isfinite(value) ? std::clamp(value, -1.0, 1.0) : 0.0;
        if (std::abs(value) < deadband) {
            return 0.0;
        }

        double sign = (value > 0) ? 1.0 : -1.0;
        double normalized = (std::abs(value) - deadband) / (1.0 - deadband);
        double curved = std::pow(normalized, joy_tuning::EXPONENT);

        return sign * curved;
    }

    /** Speed-mode multiplier on the motion_control caps, clamped to the range the robot's own
     *  preset table spans. Above 1.0 it exceeds the configured caps -- that is what the "Mad"
     *  mode is -- so the ceiling comes from the published presets rather than being open-ended.
     *  Any client can write this, and std::clamp passes NaN through, so the finite check is not
     *  redundant. */
    double drive_speed_scale() {
        const double value = this->get_parameter("motion_control.speed_scale").as_double();
        if (!std::isfinite(value)) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 10000,
                                 "motion_control.speed_scale must be finite (got %.3f); using %.3f.", value,
                                 joy_tuning::SPEED_SCALE);
            return joy_tuning::SPEED_SCALE;
        }
        return std::clamp(value, 0.05, joy_tuning::max_speed_scale());
    }

    /** Read a drive_smoothing tunable, falling back to its default unless finite and positive.
     *  Every knob here is divided by or clamped to, so 0 breaks the ramp rather than removing a
     *  limit -- a zero deceleration limit would republish the last command forever while holding
     *  the top-priority mux slot. Note 0 *does* mean "disabled" for mars_arm's max_jerk. */
    double smoothing_param(const char* name, double fallback) {
        const double value = this->get_parameter(name).as_double();
        if (std::isfinite(value) && value > 0.0) {
            return value;
        }
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 10000,
                             "%s must be finite and positive (got %.3f); using %.3f. Neither 0 nor inf disables "
                             "this limit.",
                             name, value, fallback);
        return fallback;
    }

    /**
     * Advance one velocity axis toward `target` by `dt`: a first-order lag of `time_constant`,
     * clamped to an acceleration limit, with the acceleration itself slewed at `max_jerk`.
     *
     * Far from the target the clamp dominates and the axis ramps at constant acceleration;
     * close to it the lag rounds the corner off. `applied_rate` carries the acceleration
     * across calls, so the accel -> decel handover on stick release is continuous instead of
     * a step. Deceleration gets its own, larger limit.
     */
    static double step_axis(double current, double target, double& applied_rate, double max_acceleration,
                            double max_deceleration, double max_jerk, double time_constant, double dt,
                            double settle_epsilon) {
        const bool decelerating = std::abs(target) < std::abs(current) || current * target < 0.0;
        const double limit = decelerating ? max_deceleration : max_acceleration;

        const double requested_rate = std::clamp((target - current) / time_constant, -limit, limit);
        const double max_rate_step = max_jerk * dt;
        applied_rate += std::clamp(requested_rate - applied_rate, -max_rate_step, max_rate_step);

        double next = current + applied_rate * dt;

        // Euler integration can step past the target, and the lag never quite arrives at it.
        // Settling exactly is what lets the timer recognise "stopped" and fall silent. Clear
        // the carried acceleration too, so the next move starts from rest.
        const bool overshot = (target >= current) ? next > target : next < target;
        if (overshot) {
            next = target;
            applied_rate = 0.0;
        }
        if (std::abs(next - target) < settle_epsilon) {
            next = target;
            applied_rate = 0.0;
        }
        return next;
    }

    /** Preset the current robot activity wants, or 0 when nothing applies. */
    double policy_scale() const {
        if (nav_mode_ == "mapping") {
            return joy_tuning::scale_for_mode(joy_tuning::MAPPING_MODE_SCALE_ID);
        }
        if (recording_) {
            return joy_tuning::scale_for_mode(joy_tuning::RECORDING_SCALE_ID);
        }
        return 0.0;
    }

    /**
     * Nudge speed_scale when the robot enters or leaves an activity with a preferred speed.
     *
     * Only ever acts on a *transition*, so an operator who picks a different mode mid-map
     * keeps it. And it only restores what it set: speed_scale is global, so another client
     * may have changed it meanwhile, and clobbering that would be worse than leaving the
     * robot slow.
     */
    void apply_speed_policy() {
        const double desired = policy_scale();
        if (desired == policy_desired_) {
            return;  // no transition
        }
        const double previous_desired = policy_desired_;
        policy_desired_ = desired;

        const double current = this->get_parameter("motion_control.speed_scale").as_double();
        if (policy_owns_scale_ && std::abs(current - previous_desired) > 1e-9) {
            policy_owns_scale_ = false;  // someone else moved it; it is theirs now
        }

        if (!policy_owns_scale_) {
            if (desired == 0.0) {
                return;  // nothing to enter, nothing of ours to restore
            }
            policy_restore_scale_ = current;  // entering afresh: remember what to give back
            policy_owns_scale_ = true;
        }

        const double next = (desired > 0.0) ? desired : policy_restore_scale_;
        if (desired == 0.0) {
            policy_owns_scale_ = false;
        }
        if (std::abs(next - current) < 1e-9) {
            return;
        }
        this->set_parameter(rclcpp::Parameter("motion_control.speed_scale", next));
        RCLCPP_INFO(this->get_logger(), "Speed policy: %s -> speed_scale %.2f",
                    desired > 0.0 ? (nav_mode_ == "mapping" ? "mapping" : "recording") : "idle", next);
    }

    void odom_callback(const nav_msgs::msg::Odometry::SharedPtr msg) {
        const auto& q = msg->pose.pose.orientation;
        odom_yaw_ = std::atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z));
        odom_yaw_time_ = this->now().seconds();
    }

    /** Shortest signed rotation from `from` to `to`, wrapped to [-pi, pi]. */
    static double angle_difference(double from, double to) {
        return std::atan2(std::sin(to - from), std::cos(to - from));
    }

    /**
     * Stretch a round stick gate onto a square one, keeping direction and radial proportion.
     *
     * Touch and on-screen sticks clamp to a circle, so a full-deflection diagonal is only
     * 0.71 on each axis -- and apply_curve squares each axis independently, turning that
     * into 43% of both throttle and steering. Holding a turn cost more than half the speed,
     * while leaving the turn radius unchanged, so it only made the same arc slower. The
     * keyboard gates its axes independently and never had this; this brings the sticks in
     * line rather than leaving the two input paths behaving differently.
     *
     * Axis-aligned input is untouched (scale is exactly 1), and apply_curve clamps, so the
     * keyboard's already-square vector passes through unchanged.
     */
    static void square_stick(double& x, double& y) {
        const double largest = std::max(std::abs(x), std::abs(y));
        const double magnitude = std::hypot(x, y);
        if (largest < 1e-6 || !std::isfinite(magnitude)) {
            return;
        }
        // Only stretch input that came from a round gate. A circular stick can never exceed
        // magnitude 1, so anything above that was already square-gated -- the keyboard, which
        // sends (-1, 1) on a diagonal. Stretching that too would push it past the corner and
        // apply_curve would clamp it back, which is invisible at full deflection but silently
        // erases Shift precision mode: (-0.75, 0.75) became (-1.06, 1.06) and clamped to full.
        if (magnitude > 1.0 + 1e-6) {
            return;
        }
        const double scale = magnitude / largest;
        x *= scale;
        y *= scale;
    }

    /**
     * Yaw correction to add to the published twist while driving straight.
     *
     * Engages on the *requested* yaw reaching zero, not the smoothed one. Testing the
     * smoothed value meant the hold stayed disengaged for the whole ramp-down after a turn
     * -- 460 ms and 12 deg of rotation -- and then latched the heading it had drifted to,
     * adopting the overshoot as its target. Latching on the request instead captures the
     * heading the operator stopped turning at, so the residual is corrected rather than
     * accepted.
     *
     * The latch drops whenever a turn is requested, the robot reverses (the caster flips,
     * so the old target means nothing), it stops, or /odom goes stale.
     *
     * Applied after the smoother, deliberately, so the smoother's lag is not inside the
     * feedback loop. It is slew-limited because nothing else would stop it stepping when
     * the hold engages or drops out.
     */
    /** Non-negative heading_hold tunable. Unlike smoothing_param, 0 is legitimate here --
     *  a zero deadband or engage threshold means "always", not "broken". */
    double heading_param(const char* name, double fallback) {
        const double value = this->get_parameter(name).as_double();
        return (std::isfinite(value) && value >= 0.0) ? value : fallback;
    }

    double heading_hold_correction(double linear, double requested_angular, double now, double dt) {
        const double gain = this->get_parameter("heading_hold.gain").as_double();
        const double max_correction = this->get_parameter("heading_hold.max_correction").as_double();
        const double straight_yaw = heading_param("heading_hold.straight_yaw", joy_tuning::HEADING_STRAIGHT_YAW);
        const double min_speed = heading_param("heading_hold.min_speed", joy_tuning::HEADING_MIN_SPEED);

        const double drive_sign = (linear >= 0.0) ? 1.0 : -1.0;

        // An explicit turn request releases the correction outright rather than slewing it
        // out. The correction had been opposing the caster, and easing it away over
        // HEADING_SLEW cancels roughly the first 150 ms of the turn just asked for -- the
        // turn ramps in at max_angular_acceleration over about the same time, so the two
        // sum to nothing and the robot reads as ignoring the stick. Stepping it to zero is
        // safe here precisely because a much larger commanded turn is arriving on top.
        if (std::abs(requested_angular) >= straight_yaw) {
            heading_latched_ = false;
            heading_correction_ = 0.0;
            heading_drive_sign_ = drive_sign;
            return 0.0;
        }

        const bool usable = std::isfinite(gain) && gain > 0.0 && std::isfinite(max_correction) &&
                            max_correction > 0.0 && (now - odom_yaw_time_) < joy_tuning::HEADING_FEEDBACK_TIMEOUT &&
                            std::abs(linear) > min_speed && drive_sign == heading_drive_sign_;
        heading_drive_sign_ = drive_sign;

        double target = 0.0;
        if (!usable) {
            heading_latched_ = false;
        } else if (!heading_latched_) {
            heading_target_ = odom_yaw_;
            heading_latched_ = true;
        } else {
            // Bleed the target toward the measured heading, so the hold only remembers the
            // last `leak` seconds. Without this it is an absolute lock: it drags the robot
            // back to wherever the turn ended long after the operator has moved on, which is
            // not wanted -- an operator corrects their own overshoot. It also let the error
            // grow unbounded while the caster pushed, until the correction sat saturated.
            // What survives is the part we do want: resistance to being turned *now*.
            const double leak = this->get_parameter("heading_hold.leak").as_double();
            if (std::isfinite(leak) && leak > 0.0) {
                heading_target_ += angle_difference(heading_target_, odom_yaw_) * std::min(1.0, dt / leak);
            }
            // /odom yaw and published angular.z share a sign: measured over 1274 ticks,
            // commanding +49 deg/s yields +52 deg/s and -54 yields -53, so the two agree to
            // within 5%. Reversing this term makes the loop positive feedback.
            const double error = angle_difference(odom_yaw_, heading_target_);
            if (std::abs(error) > heading_param("heading_hold.deadband", joy_tuning::HEADING_DEADBAND)) {
                target = std::clamp(gain * error, -max_correction, max_correction);
            }
        }

        const double step = heading_param("heading_hold.slew", joy_tuning::HEADING_SLEW) * dt;
        heading_correction_ += std::clamp(target - heading_correction_, -step, step);
        return heading_correction_;
    }

    void joystick_callback(const geometry_msgs::msg::Vector3::SharedPtr msg) {
        double stick_x = msg->x;  // steering
        double stick_y = msg->y;  // throttle
        square_stick(stick_x, stick_y);

        double shaped_x = apply_curve(stick_x);
        double shaped_y = apply_curve(stick_y);

        // Scale after the curve, never before: scaling the raw axis pushes the stick range
        // down toward the deadband and costs most of its resolution.
        const double scale = drive_speed_scale();
        double linear = shaped_y * this->get_parameter("motion_control.max_speed").as_double() * scale;
        double angular = -shaped_x * this->get_parameter("motion_control.max_angular_speed").as_double() * scale;

        if (this->get_parameter("reverse_steering").as_bool() && linear < 0.0) {
            // Invert steering while reversing, ramped over REVERSE_STEER_RAMP to avoid a snap at zero throttle.
            // The ramp scales with the speed mode so the blend still completes at full reverse stick.
            double blend = std::min(1.0, -linear / (joy_tuning::REVERSE_STEER_RAMP * scale));
            angular *= (1.0 - 2.0 * blend);
        }

        // Publishing belongs to drive_smoother_callback; this only moves the target it ramps toward.
        target_linear_ = linear;
        target_angular_ = angular;
        last_joystick_time_ = this->now().seconds();
        drive_active_ = true;
    }

    /**
     * Ramp the published teleop twist toward the joystick target at a fixed rate.
     *
     * Publishes only while driving. Teleop is top priority in the cmd_vel mux, so streaming
     * zeros forever would pin the slot and starve skills and nav; once settled at rest this
     * goes quiet until the next /joystick message.
     */
    void drive_smoother_callback() {
        const double now = this->now().seconds();
        const bool input_fresh =
            (now - last_joystick_time_) < smoothing_param("motion_control.input_timeout", joy_tuning::INPUT_TIMEOUT);

        // Stale input (app backgrounded, Wi-Fi drop) is a stop request, not a hold: ramp down
        // under our own limit rather than let the mux window expire into a hard zero.
        const double target_linear = input_fresh ? target_linear_ : 0.0;
        const double target_angular = input_fresh ? target_angular_ : 0.0;

        const bool at_rest =
            target_linear == 0.0 && target_angular == 0.0 && smoothed_linear_ == 0.0 && smoothed_angular_ == 0.0;
        if (!drive_active_ && at_rest) {
            return;
        }

        // The dt the timer was built with, not a live re-read: motion_control.rate fixes the
        // callback cadence at construction, so recomputing it here would scale the real
        // acceleration without changing how often we run.
        const double dt = smoothing_dt_;
        const double speed_tc = smoothing_param("motion_control.speed_time_constant", joy_tuning::SPEED_TIME_CONSTANT);
        const double angular_tc =
            smoothing_param("motion_control.angular_speed_time_constant", joy_tuning::ANGULAR_SPEED_TIME_CONSTANT);
        // Limits scale with the speed mode, so slow mode is a lower ceiling, not a snappier ramp.
        const double scale = drive_speed_scale();
        // Not scaled: this one is a floor set by the wire, which quantises to int(v * 100)
        // regardless of speed mode.
        const double settle = smoothing_param("motion_control.settle_epsilon", joy_tuning::SETTLE_EPSILON);

        smoothed_linear_ =
            step_axis(smoothed_linear_, target_linear, rate_linear_,
                      smoothing_param("motion_control.max_acceleration", joy_tuning::MAX_ACCELERATION) * scale,
                      smoothing_param("motion_control.max_deceleration", joy_tuning::MAX_DECELERATION) * scale,
                      smoothing_param("motion_control.max_jerk", joy_tuning::MAX_JERK) * scale, speed_tc, dt, settle);
        smoothed_angular_ = step_axis(
            smoothed_angular_, target_angular, rate_angular_,
            smoothing_param("motion_control.max_angular_acceleration", joy_tuning::MAX_ANGULAR_ACCELERATION) * scale,
            smoothing_param("motion_control.max_angular_deceleration", joy_tuning::MAX_ANGULAR_DECELERATION) * scale,
            smoothing_param("motion_control.max_angular_jerk", joy_tuning::MAX_ANGULAR_JERK) * scale, angular_tc, dt,
            settle);

        const double correction = heading_hold_correction(smoothed_linear_, target_angular, now, dt);

        geometry_msgs::msg::Twist twist_msg;
        twist_msg.linear.x = smoothed_linear_;
        twist_msg.linear.y = 0.0;
        twist_msg.linear.z = 0.0;
        twist_msg.angular.x = 0.0;
        twist_msg.angular.y = 0.0;
        twist_msg.angular.z = smoothed_angular_ + correction;
        cmd_vel_pub_->publish(twist_msg);

        // That publish was the settling zero, so the next tick can stop publishing. The
        // correction has to be spent too, or we would fall silent still holding one.
        if (target_linear == 0.0 && target_angular == 0.0 && smoothed_linear_ == 0.0 && smoothed_angular_ == 0.0 &&
            correction == 0.0) {
            drive_active_ = false;
        }
    }

    /**
     * Receives leader positions and converts to radians:
     *   - Conversion: (position - 2048) * (2 * pi / 4096)
     * Then publishes the transformed values as commands.
     *
     * Note: Offset application is currently disabled:
     *   - Offsets: [-1024, 1024, 0, -1024, -1024, 0]
     */
    void leader_positions_callback(const std_msgs::msg::Int32MultiArray::SharedPtr msg) {
        const size_t expected_length = 6;  // Adjust if your leader arm has a different number of joints

        if (msg->data.size() != expected_length) {
            RCLCPP_ERROR(this->get_logger(), "Received %zu positions; expected %zu.", msg->data.size(),
                         expected_length);
            return;
        }

        // Convert positions to radians
        // Offset application (currently disabled)
        // offsets = [-1024, 1024, 0, -1024, -1024, 0]
        // positions_corrected = positions + offsets

        // Convert to radians:
        // positions_rad = (positions_corrected - 2048) * (2 * pi / 4096)
        std_msgs::msg::Float64MultiArray cmd_msg;
        cmd_msg.data.resize(expected_length);

        for (size_t i = 0; i < expected_length; ++i) {
            cmd_msg.data[i] = (static_cast<double>(msg->data[i]) - 2048.0) * (2.0 * M_PI / 4096.0);
        }

        // Publish the transformed command
        cmd_pub_->publish(cmd_msg);
    }

    /**
     * Reads robot_info.json and os_config.json, extracts specified keys, and publishes them as a JSON string.
     * - robot_name: from robot_info.json
     * - minimum_app_version: from os_config.json
     * - wifi_ssid: gets the current WiFi SSID from NetworkManager
     * - version: from git
     * - tag_subject: the subject (first line) of the latest git tag's annotation
     * - tag_body: the body (rest of message) of the latest git tag's annotation
     * - device_id: Bluetooth MAC address from system
     * - update_available: true if system updates are available (from innate-update)
     * Logs errors if file/JSON processing fails or keys are missing.
     * Publishes "{}" if no keys are found or an error occurs.
     */
    void publish_robot_info_callback() {
        json data_to_publish_dict;
        std::string final_json_string_to_publish = "{}";

        try {
            // Get robot info (creates file with defaults if needed)
            json robot_info = get_robot_info();
            std::string default_hw_rev = this->get_parameter("default_hardware_revision").as_string();

            data_to_publish_dict["robot_name"] = robot_info.value("robot_name", "MARS");
            if (robot_info.contains("robot_id") && !robot_info["robot_id"].is_null()) {
                data_to_publish_dict["robot_id"] = robot_info["robot_id"];
            } else {
                data_to_publish_dict["robot_id"] = nullptr;
            }
            data_to_publish_dict["hardware_revision"] = robot_info.value("hardware_revision", default_hw_rev);
            data_to_publish_dict["color_variant"] = robot_info.value("color_variant", "black");
            data_to_publish_dict["volume_percent"] = robot_info.value("volume_percent", 80);
            data_to_publish_dict["microphone_enabled"] = robot_info.value("microphone_enabled", true);
            // Speed mode is global robot state, so every client (app and webapp) mirrors it
            // from here rather than trusting its own last write.
            data_to_publish_dict["drive_speed_scale"] = drive_speed_scale();
            // ...and the presets themselves, so the robot is the single source of truth for
            // what the pickers offer. Previously each client carried its own copy of the
            // table with a "keep in sync" comment, which meant adding a mode needed two
            // client releases to stay consistent. Clients that predate this fall back to
            // their built-in table.
            data_to_publish_dict["drive_speed_modes"] = json::array();
            for (const auto& mode : joy_tuning::SPEED_MODES) {
                data_to_publish_dict["drive_speed_modes"].push_back(
                    {{"id", mode.id}, {"label", mode.label}, {"scale", mode.scale}});
            }

            // Read minimum_app_version from os_config.json
            if (app_config_.contains("minimum_app_version")) {
                data_to_publish_dict["minimum_app_version"] = app_config_["minimum_app_version"];
            }

            // Include WiFi SSID
            std::string wifi_ssid = get_cached_wifi_ssid();
            if (!wifi_ssid.empty()) {
                data_to_publish_dict["wifi_ssid"] = wifi_ssid;
            }

            // Include robot version and tag info
            try {
                std::string mars_root = get_mars_root();
                std::string robot_version = get_robot_version(mars_root);
                data_to_publish_dict["version"] = robot_version;

                // Include tag subject and body for the latest version
                std::string tag_subject = get_tag_subject(mars_root);
                if (!tag_subject.empty()) {
                    data_to_publish_dict["tag_subject"] = tag_subject;
                }

                std::string tag_body = get_tag_body(mars_root);
                if (!tag_body.empty()) {
                    data_to_publish_dict["tag_body"] = tag_body;
                }
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Failed to get robot version: %s", e.what());
            }

            // Include Bluetooth device ID
            std::string device_id = get_bluetooth_device_id();
            if (!device_id.empty()) {
                data_to_publish_dict["device_id"] = device_id;
            }

            // Include system hostname
            std::string hostname = get_hostname();
            if (!hostname.empty()) {
                data_to_publish_dict["hostname"] = hostname;
            }

            // Include update availability status
            data_to_publish_dict["update_available"] = get_cached_update_available();

            // Include update running status
            data_to_publish_dict["update_running"] = check_update_running();

            data_to_publish_dict["supports_webrtc_audio"] = true;
            data_to_publish_dict["supports_digital_skills"] = true;

            if (!data_to_publish_dict.empty()) {
                final_json_string_to_publish = data_to_publish_dict.dump();
            }

        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error processing robot info: %s", e.what());
            // final_json_string_to_publish remains "{}" as initialized
        }

        std_msgs::msg::String msg;
        msg.data = final_json_string_to_publish;
        robot_info_pub_->publish(msg);
    }

    /**
     * Timer callback to sync system hostname with robot name from robot_info.json.
     * Runs every 30 seconds to ensure hostname stays in sync.
     */
    void sync_hostname_callback() {
        try {
            // Read robot_info from file
            json robot_info = get_robot_info();

            // Get robot name from JSON
            std::string robot_name = robot_info.value("robot_name", "");
            if (robot_name.empty()) {
                robot_name = "MARS";
            }

            // Get current system hostname
            std::string current_hostname = get_system_hostname();
            std::string sanitized_robot_name = sanitize_hostname(robot_name);

            // Only update if they don't match
            if (current_hostname != sanitized_robot_name) {
                std::string error_msg;
                if (set_system_hostname(sanitized_robot_name, error_msg)) {
                    RCLCPP_INFO(this->get_logger(), "Successfully synced hostname to '%s'",
                                sanitized_robot_name.c_str());
                } else {
                    RCLCPP_WARN(this->get_logger(), "Failed to sync hostname: %s", error_msg.c_str());
                }
            }

        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error syncing hostname: %s", e.what());
        }
    }

    /**
     * Service callback to change the robot name in robot_info.json.
     */
    void set_robot_name_callback(const std::shared_ptr<mars_msgs::srv::SetRobotName::Request> request,
                                 std::shared_ptr<mars_msgs::srv::SetRobotName::Response> response) {
        try {
            // Load current robot_info
            json robot_info = get_robot_info();

            // Update robot name
            std::string old_name = robot_info.value("robot_name", "Not set");
            robot_info["robot_name"] = request->robot_name;

            // Save updated robot_info
            if (!set_robot_info(robot_info)) {
                response->success = false;
                response->message = "Failed to save robot_info.json";
                return;
            }

            response->success = true;
            response->message = "Robot name changed from '" + old_name + "' to '" + request->robot_name + "'";

            RCLCPP_INFO(this->get_logger(), "%s", response->message.c_str());

        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed to change robot name: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "%s", response->message.c_str());
        }
    }

    /**
     * Service callback to trigger a system update.
     * Currently disabled - users should SSH into the robot and run innate-update manually.
     */
    void trigger_update_callback(const std::shared_ptr<mars_msgs::srv::TriggerUpdate::Request> request,
                                 std::shared_ptr<mars_msgs::srv::TriggerUpdate::Response> response) {
        (void)request;  // Unused for now

        RCLCPP_WARN(this->get_logger(), "Update trigger via service not implemented");

        response->success = false;
        response->message = "Not implemented. SSH into the robot and run: innate-update apply";
    }

    /**
     * Service callback to shutdown the Jetson.
     * Uses 'sudo shutdown' command with optional delay.
     */
    void shutdown_callback(const std::shared_ptr<mars_msgs::srv::Shutdown::Request> request,
                           std::shared_ptr<mars_msgs::srv::Shutdown::Response> response) {
        try {
            int delay = request->delay_seconds;
            if (delay < 0) {
                delay = 0;
            }

            std::string shutdown_cmd;
            if (delay == 0) {
                shutdown_cmd = "sudo shutdown now";
            } else {
                shutdown_cmd = "sudo shutdown +" + std::to_string(delay / 60 + 1);  // shutdown uses minutes
            }

            RCLCPP_WARN(this->get_logger(), "Shutdown requested with delay: %d seconds. Executing: %s", delay,
                        shutdown_cmd.c_str());

            // Execute shutdown command in background so we can respond first
            std::string bg_cmd = shutdown_cmd + " &";
            int result = std::system(bg_cmd.c_str());

            if (result == 0) {
                response->success = true;
                response->message =
                    "Shutdown initiated" + (delay > 0 ? " with delay of " + std::to_string(delay) + " seconds" : "");
                RCLCPP_INFO(this->get_logger(), "%s", response->message.c_str());
            } else {
                response->success = false;
                response->message = "Failed to execute shutdown command (exit code: " + std::to_string(result) + ")";
                RCLCPP_ERROR(this->get_logger(), "%s", response->message.c_str());
            }

        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed to initiate shutdown: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "%s", response->message.c_str());
        }
    }

    /**
     * Apply volume to ALSA default playback device.
     * This updates the system volume immediately, affecting any currently
     * playing audio (e.g. TTS via aplay) in real time.
     * -M maps the percentage on alsamixer's perceptual (dB-normalized)
     * scale instead of the raw register scale, so e.g. 30% is clearly
     * audible rather than ~-70dB (INN-467).
     */
    void apply_alsa_volume(int percent) {
        // Remap 0-100 onto the audible [AUDIBLE_FLOOR, 100] band; the speaker is
        // silent across the lower range even with -M. 0 still mutes.
        constexpr int AUDIBLE_FLOOR = 55;
        int mixer_percent;
        if (percent <= 0) {
            mixer_percent = 0;
        } else if (percent >= 100) {
            mixer_percent = 100;
        } else {
            mixer_percent = AUDIBLE_FLOOR + (100 - AUDIBLE_FLOOR) * percent / 100;
        }
        std::string cmd = "amixer -M sset Master " + std::to_string(mixer_percent) + "% 2>/dev/null";
        int ret = std::system(cmd.c_str());
        if (ret != 0) {
            RCLCPP_WARN(this->get_logger(), "amixer sset Master failed (rc=%d)", ret);
        }
    }

    /**
     * Service callback to set the system speaker volume.
     * Stores volume_percent in robot_info.json and applies it to
     * ALSA immediately so in-progress audio is affected.
     */
    void set_volume_callback(const std::shared_ptr<mars_msgs::srv::SetVolume::Request> request,
                             std::shared_ptr<mars_msgs::srv::SetVolume::Response> response) {
        int vol = request->volume_percent;
        if (vol < 0 || vol > 100) {
            response->success = false;
            response->message = "Volume must be between 0 and 100";
            return;
        }

        try {
            json robot_info = get_robot_info();
            robot_info["volume_percent"] = vol;

            if (!set_robot_info(robot_info)) {
                response->success = false;
                response->message = "Failed to save robot_info.json";
                return;
            }

            // Apply to ALSA immediately (affects in-progress playback)
            apply_alsa_volume(vol);

            response->success = true;
            response->message = "Volume set to " + std::to_string(vol) + "%";
            RCLCPP_INFO(this->get_logger(), "%s", response->message.c_str());
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed to set volume: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "%s", response->message.c_str());
        }
    }

    void publish_mic_enabled(bool enabled) {
        std_msgs::msg::Bool msg;
        msg.data = enabled;
        mic_enabled_pub_->publish(msg);
    }

    /**
     * Service callback to enable/disable the robot microphone.
     * Stores microphone_enabled in robot_info.json and broadcasts the new
     * state on the latched /microphone_enabled topic; the input manager
     * closes/reopens the mic input device accordingly.
     */
    void set_microphone_callback(const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
                                 std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
        try {
            json robot_info = get_robot_info();
            robot_info["microphone_enabled"] = request->data;

            if (!set_robot_info(robot_info)) {
                response->success = false;
                response->message = "Failed to save robot_info.json";
                return;
            }

            publish_mic_enabled(request->data);

            response->success = true;
            response->message = std::string("Microphone ") + (request->data ? "enabled" : "disabled");
            RCLCPP_INFO(this->get_logger(), "%s", response->message.c_str());
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed to set microphone: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "%s", response->message.c_str());
        }
    }

    // Member variables
    json app_config_;

    // Cache for WiFi SSID to avoid frequent subprocess calls
    std::string _cached_wifi_ssid;
    double _last_wifi_check_time;
    double _wifi_check_interval;

    // Cache for update check to avoid frequent subprocess calls
    bool _cached_update_available;
    double _last_update_check_time;
    double _update_check_interval;

    // Subscribers
    rclcpp::Subscription<geometry_msgs::msg::Vector3>::SharedPtr joystick_sub_;
    rclcpp::Subscription<std_msgs::msg::Int32MultiArray>::SharedPtr leader_sub_;
    rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr odom_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr nav_mode_sub_;
    rclcpp::Subscription<brain_messages::msg::RecorderStatus>::SharedPtr recorder_sub_;

    // Publishers
    rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr cmd_vel_pub_;
    rclcpp::Publisher<std_msgs::msg::Float64MultiArray>::SharedPtr cmd_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr robot_info_pub_;
    rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr mic_enabled_pub_;

    // Timers
    rclcpp::TimerBase::SharedPtr robot_info_timer_;
    rclcpp::TimerBase::SharedPtr hostname_sync_timer_;
    rclcpp::TimerBase::SharedPtr drive_smoother_timer_;

    // Drive smoother state. Only touched from joystick_callback and drive_smoother_callback,
    // which the single-threaded executor serialises, so no locking is needed.
    double target_linear_ = 0.0;
    double target_angular_ = 0.0;
    double smoothed_linear_ = 0.0;
    double smoothed_angular_ = 0.0;
    // Acceleration carried between ticks so the jerk limit has something to slew from.
    double rate_linear_ = 0.0;
    double rate_angular_ = 0.0;
    double last_joystick_time_ = 0.0;
    bool drive_active_ = false;
    // Heading hold. odom_yaw_ is written from the /odom callback, which the single-threaded
    // executor serialises against the smoother timer.
    double odom_yaw_ = 0.0;
    double odom_yaw_time_ = 0.0;
    double heading_target_ = 0.0;
    double heading_correction_ = 0.0;
    double heading_drive_sign_ = 0.0;
    bool heading_latched_ = false;
    // Speed policy state. policy_desired_ is the scale the current activity wants (0 = none);
    // policy_owns_scale_ tracks whether the value on the robot is still the one we set.
    std::string nav_mode_;
    bool recording_ = false;
    double policy_desired_ = 0.0;
    double policy_restore_scale_ = 1.0;
    bool policy_owns_scale_ = false;
    // Integration step, fixed to the period the smoother timer was created with.
    double smoothing_dt_ = joy_tuning::CONTROL_DT;

    // Services
    rclcpp::Service<mars_msgs::srv::SetRobotName>::SharedPtr set_robot_name_srv_;
    rclcpp::Service<mars_msgs::srv::TriggerUpdate>::SharedPtr trigger_update_srv_;
    rclcpp::Service<mars_msgs::srv::Shutdown>::SharedPtr shutdown_srv_;
    rclcpp::Service<mars_msgs::srv::SetVolume>::SharedPtr set_volume_srv_;
    rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr set_microphone_srv_;
};

}  // namespace mars_control

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<mars_control::AppControl>();
    try {
        rclcpp::spin(node);
    } catch (const std::exception& e) {
        RCLCPP_ERROR(node->get_logger(), "Unhandled exception in app node: %s", e.what());
    }
    rclcpp::shutdown();
    return 0;
}
