// Joystick shaping — ported from mars_control's app.cpp joy_tuning /
// joystick_callback (the real robot's /joystick -> /cmd_vel path), so the
// standalone sim feels like the real teleop. Reverse-steering inversion
// (off by default on the robot) is intentionally omitted here.

const DEADZONE = 0.15;
const EXPONENT = 2.0; // quadratic; 1.0 would be linear
export const MAX_LINEAR = 0.4; // m/s
export const MAX_ANGULAR = 1.0; // rad/s

/** Apply deadband + a power-law response curve to a single [-1, 1] axis. */
function applyCurve(value: number, deadband = DEADZONE): number {
  const magnitude = Math.abs(value);
  if (magnitude < deadband) return 0;
  const sign = value > 0 ? 1 : -1;
  const normalized = (magnitude - deadband) / (1 - deadband);
  return sign * Math.pow(normalized, EXPONENT);
}

/**
 * Joystick-frame input (x = steering, y = throttle, both in [-1, 1]) to a
 * commanded (linear, angular) velocity in m/s / rad/s.
 */
export function joystickToTwist(x: number, y: number): { linear: number; angular: number } {
  const shapedX = applyCurve(x);
  const shapedY = applyCurve(y);
  return {
    linear: shapedY * MAX_LINEAR,
    angular: -shapedX * MAX_ANGULAR,
  };
}
