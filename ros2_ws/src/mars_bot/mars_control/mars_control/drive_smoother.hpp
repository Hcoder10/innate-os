// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
/**
 * Teleop drive shaping: stick vector -> smoothed twist.
 *
 * Pure logic, no ROS. app.cpp reads the parameters and owns the timer; this owns the
 * ramp state and the heading-hold loop.
 */
#ifndef MARS_CONTROL_DRIVE_SMOOTHER_HPP
#define MARS_CONTROL_DRIVE_SMOOTHER_HPP

#include <array>
#include <string>

namespace mars_control::drive {

// Stick shaping.
constexpr double DEADZONE = 0.15;
constexpr double EXPONENT = 2.0;             // quadratic; 1.0 = linear
constexpr double MAX_LINEAR = 0.4;           // m/s
constexpr double MAX_ANGULAR = 1.0;          // rad/s
constexpr double REVERSE_STEER_RAMP = 0.15;  // m/s, blend width for `reverse_steering`

// Ramp defaults. Same lag-plus-slew shape as JoystickAxis in joystick.py, with
// deceleration and jerk limited separately.
constexpr double CONTROL_DT = 0.02;                   // s, 50 Hz
constexpr double SPEED_TIME_CONSTANT = 0.40;          // s
constexpr double ANGULAR_SPEED_TIME_CONSTANT = 0.10;  // s
constexpr double MAX_ACCELERATION = 0.2;              // m/s^2
constexpr double MAX_DECELERATION = 1.2;              // m/s^2
constexpr double MAX_ANGULAR_ACCELERATION = 2.0;      // rad/s^2
constexpr double MAX_ANGULAR_DECELERATION = 6.0;      // rad/s^2
constexpr double MAX_JERK = 10.0;                     // m/s^3
constexpr double MAX_ANGULAR_JERK = 100.0;            // rad/s^3
// Keep time_constant >= max_deceleration / (2 * max_jerk) per axis, or the axis arrives at
// rest still braking and that torque is cut in one tick — a kick after it has stopped.

constexpr double INPUT_TIMEOUT = 0.4;      // s
constexpr double MUX_TELEOP_WINDOW = 0.5;  // s, cmd_vel_mux's freshness window; INPUT_TIMEOUT's ceiling
// bringup sends int(v * 100), so 0.01 is one wire count and anything finer is motion the
// motors cannot express.
constexpr double SETTLE_EPSILON = 0.01;  // m/s and rad/s

// Heading hold. Nothing else closes the yaw loop: the wheels track their RPM setpoints
// while the caster still yaws the robot. Proportional only — /odom heading is quantised to
// 0.01 rad, which suits a term that integrates and ruins one that differentiates.
constexpr double HEADING_GAIN = 5.0;
constexpr double HEADING_MAX_CORRECTION = 1.0;  // rad/s
// Seconds of memory. Bounds drift to roughly disturbance / (1 + gain * leak) without
// restoring heading lost earlier; 0 makes it an absolute lock.
constexpr double HEADING_LEAK = 0.2;              // s
constexpr double HEADING_DEADBAND = 0.01;         // rad, one odom quantum
constexpr double HEADING_SLEW = 2.0;              // rad/s^2
constexpr double HEADING_STRAIGHT_YAW = 0.05;     // rad/s
constexpr double HEADING_MIN_SPEED = 0.01;        // m/s
constexpr double HEADING_FEEDBACK_TIMEOUT = 0.3;  // s

// Speed modes. Published on /robot/info so clients render the robot's set rather than
// their own copy. Mad exceeds 1.0, so the clamp reads this table's largest entry rather
// than a literal.
struct SpeedMode {
    const char* id;
    const char* label;
    double scale;
};
constexpr std::array<SpeedMode, 4> SPEED_MODES{
    {{"slow", "Slow", 0.35}, {"medium", "Med", 0.65}, {"fast", "Fast", 1.0}, {"mad", "Mad", 2.0}}};
constexpr const char* MAD_MODE_ID = "mad";
constexpr const char* RECORDING_SCALE_ID = "medium";
constexpr double SPEED_SCALE = 1.0;

// Mad states its accelerations outright: it wants 2.0 m/s^2 where scaling gives 0.4 and
// 3.0 rad/s^2 where scaling gives 4.0, and one ratio cannot be both.
constexpr double MAD_MAX_ACCELERATION = 2.0;          // m/s^2
constexpr double MAD_MAX_ANGULAR_ACCELERATION = 3.0;  // rad/s^2

double scale_for_mode(const char* id);
double max_speed_scale();
bool is_mad_scale(double scale);

/** Deadband plus power-law response. Input is clamped: /joystick is open and the curve amplifies. */
double apply_curve(double value, double deadband = DEADZONE);

/**
 * Stretch a round stick gate onto a square one, keeping direction and radial proportion.
 *
 * A circular gate gives 0.71 per axis on a full diagonal, which apply_curve squares into
 * 43% of both throttle and steering — and the turn radius is unchanged either way, so it
 * only made the same arc slower. Input already beyond magnitude 1 came from a square gate
 * (the keyboard) and is left alone; stretching it would erase Shift precision mode.
 */
void square_stick(double& x, double& y);

/** Shortest signed rotation from `from` to `to`, wrapped to [-pi, pi]. */
double angle_difference(double from, double to);

/** Per-axis ramp limits for one tick. */
struct AxisLimits {
    double max_acceleration;
    double max_deceleration;
    double max_jerk;
    double time_constant;
    double settle_epsilon;
};

/**
 * Advance one velocity axis toward `target`: first-order lag, clamped to an acceleration
 * limit, with the acceleration itself slewed at max_jerk.
 *
 * `applied_rate` carries the acceleration across calls so the accel -> decel handover on
 * stick release is continuous rather than a step.
 */
double step_axis(double current, double target, double& applied_rate, const AxisLimits& limits, double dt);

/** Live heading-hold tuning, read from parameters each tick. */
struct HeadingTuning {
    double gain;
    double max_correction;
    double leak;
    double deadband;
    double slew;
    double straight_yaw;
    double min_speed;
};

/**
 * Resists being turned off course while driving straight, without restoring heading lost
 * earlier — an operator corrects their own overshoot.
 */
class HeadingHold {
   public:
    void set_measured(double yaw, double stamp) {
        yaw_ = yaw;
        yaw_stamp_ = stamp;
    }

    /**
     * Correction to add to the published yaw rate.
     *
     * Latches on the *requested* yaw reaching zero, not the smoothed one: testing the
     * smoothed value kept the hold disengaged for the whole ramp-down after a turn, then
     * latched the heading it had drifted to.
     */
    double correction(double linear, double requested_angular, double now, double dt, const HeadingTuning& tuning);

   private:
    double yaw_ = 0.0;
    double yaw_stamp_ = 0.0;
    double target_ = 0.0;
    double applied_ = 0.0;
    double drive_sign_ = 0.0;
    bool latched_ = false;
};

/** Ramp state for both axes, plus the heading hold that rides on the angular one. */
class DriveSmoother {
   public:
    void set_target(double linear, double angular) {
        target_linear_ = linear;
        target_angular_ = angular;
    }
    double target_linear() const {
        return target_linear_;
    }
    double target_angular() const {
        return target_angular_;
    }

    double linear() const {
        return linear_;
    }
    double angular() const {
        return angular_;
    }

    /** Ramp one tick toward the given targets. */
    void step(double target_linear, double target_angular, const AxisLimits& linear_limits,
              const AxisLimits& angular_limits, double dt);

    HeadingHold& heading() {
        return heading_;
    }

    /** True once both axes have settled at rest, so the caller can fall silent. */
    bool at_rest() const {
        return linear_ == 0.0 && angular_ == 0.0;
    }

   private:
    double target_linear_ = 0.0;
    double target_angular_ = 0.0;
    double linear_ = 0.0;
    double angular_ = 0.0;
    double linear_rate_ = 0.0;
    double angular_rate_ = 0.0;
    HeadingHold heading_;
};

}  // namespace mars_control::drive

#endif  // MARS_CONTROL_DRIVE_SMOOTHER_HPP
