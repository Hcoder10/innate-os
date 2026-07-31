// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#include "drive_smoother.hpp"

#include <algorithm>
#include <cmath>

namespace mars_control::drive {

double scale_for_mode(const char* id) {
    for (const auto& mode : SPEED_MODES) {
        if (std::string(mode.id) == id) {
            return mode.scale;
        }
    }
    return 0.0;
}

double max_speed_scale() {
    double largest = 0.0;
    for (const auto& mode : SPEED_MODES) {
        largest = std::max(largest, mode.scale);
    }
    return largest;
}

bool is_mad_scale(double scale) {
    const double mad = scale_for_mode(MAD_MODE_ID);
    return mad > 0.0 && std::abs(scale - mad) < 1e-6;
}

double apply_curve(double value, double deadband) {
    value = std::isfinite(value) ? std::clamp(value, -1.0, 1.0) : 0.0;
    if (std::abs(value) < deadband) {
        return 0.0;
    }
    const double sign = (value > 0) ? 1.0 : -1.0;
    return sign * std::pow((std::abs(value) - deadband) / (1.0 - deadband), EXPONENT);
}

void square_stick(double& x, double& y) {
    const double largest = std::max(std::abs(x), std::abs(y));
    const double magnitude = std::hypot(x, y);
    if (largest < 1e-6 || !std::isfinite(magnitude) || magnitude > 1.0 + 1e-6) {
        return;
    }
    const double scale = magnitude / largest;
    x *= scale;
    y *= scale;
}

double angle_difference(double from, double to) {
    return std::atan2(std::sin(to - from), std::cos(to - from));
}

double step_axis(double current, double target, double& applied_rate, const AxisLimits& limits, double dt) {
    const bool decelerating = std::abs(target) < std::abs(current) || current * target < 0.0;
    const double limit = decelerating ? limits.max_deceleration : limits.max_acceleration;

    const double requested_rate = std::clamp((target - current) / limits.time_constant, -limit, limit);
    const double max_rate_step = limits.max_jerk * dt;
    applied_rate += std::clamp(requested_rate - applied_rate, -max_rate_step, max_rate_step);

    double next = current + applied_rate * dt;

    // Euler can step past the target and the lag never quite arrives. Settling exactly is
    // what lets the caller recognise "stopped"; clearing the rate keeps the next move from
    // launching already accelerating.
    const bool overshot = (target >= current) ? next > target : next < target;
    if (overshot || std::abs(next - target) < limits.settle_epsilon) {
        next = target;
        applied_rate = 0.0;
    }
    return next;
}

double HeadingHold::correction(double linear, double requested_angular, double now, double dt,
                              const HeadingTuning& tuning) {
    const double drive_sign = (linear >= 0.0) ? 1.0 : -1.0;

    // A turn request releases the correction outright rather than slewing it out. Easing it
    // away cancels roughly the first 150 ms of the requested turn — the turn ramps in over
    // about the same time — and reads as the robot ignoring the stick. Stepping to zero is
    // safe because a much larger commanded turn is arriving on top.
    if (std::abs(requested_angular) >= tuning.straight_yaw) {
        latched_ = false;
        applied_ = 0.0;
        drive_sign_ = drive_sign;
        return 0.0;
    }

    const bool usable = std::isfinite(tuning.gain) && tuning.gain > 0.0 && std::isfinite(tuning.max_correction) &&
                        tuning.max_correction > 0.0 && (now - yaw_stamp_) < HEADING_FEEDBACK_TIMEOUT &&
                        std::abs(linear) > tuning.min_speed && drive_sign == drive_sign_;
    drive_sign_ = drive_sign;

    double target = 0.0;
    if (!usable) {
        latched_ = false;
    } else if (!latched_) {
        target_ = yaw_;
        latched_ = true;
    } else {
        if (std::isfinite(tuning.leak) && tuning.leak > 0.0) {
            target_ += angle_difference(target_, yaw_) * std::min(1.0, dt / tuning.leak);
        }
        // /odom yaw and published angular.z share a sign — measured over 1274 ticks,
        // commanding +49 deg/s yields +52 and -54 yields -53. Reversing this makes the loop
        // positive feedback.
        const double error = angle_difference(yaw_, target_);
        if (std::abs(error) > tuning.deadband) {
            target = std::clamp(tuning.gain * error, -tuning.max_correction, tuning.max_correction);
        }
    }

    const double step = tuning.slew * dt;
    applied_ += std::clamp(target - applied_, -step, step);
    return applied_;
}

void DriveSmoother::step(double target_linear, double target_angular, const AxisLimits& linear_limits,
                         const AxisLimits& angular_limits, double dt) {
    linear_ = step_axis(linear_, target_linear, linear_rate_, linear_limits, dt);
    angular_ = step_axis(angular_, target_angular, angular_rate_, angular_limits, dt);
}

}  // namespace mars_control::drive
