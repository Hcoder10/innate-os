// RobotPose — minimal kinematic integrator for the robot base.
//
// Mirrors SimulationNode._apply_velocity_cmd in
// sim/src/simulation/simulation_node.py: no dynamics, just integrate a
// commanded (linear, angular) velocity into a 2D pose every tick. Scene is
// Z-up, X-forward (matches ROS's REP-103 convention and /odom).

export class RobotPose {
  x: number;
  y: number;
  yaw: number; // radians, 0 = facing +X

  private readonly spawn: { x: number; y: number; yaw: number };

  constructor(x = 0, y = 0, yaw = 0) {
    this.x = x;
    this.y = y;
    this.yaw = yaw;
    this.spawn = { x, y, yaw };
  }

  /** Advance the pose by one step of a commanded velocity. */
  integrate(linearX: number, angularZ: number, dt: number): void {
    this.x += Math.cos(this.yaw) * linearX * dt;
    this.y += Math.sin(this.yaw) * linearX * dt;
    this.yaw += angularZ * dt;
  }

  /** Back to the spawn pose passed into the constructor (not necessarily the origin). */
  reset(): void {
    this.x = this.spawn.x;
    this.y = this.spawn.y;
    this.yaw = this.spawn.yaw;
  }
}
