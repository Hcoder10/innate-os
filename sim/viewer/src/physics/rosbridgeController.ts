// Connected-mode state source for the webapp's SimSession: subscribe to the
// real innate-os ROS graph over rosbridge and mirror the virtual MARS
// driver's pose + joint state. Read-only -- teleop/commands go through the
// webapp's own rosbridge client, and physics lives in the mars_sim_driver
// container.
//
// Speaks the rosbridge JSON protocol directly (subscribe/publish) -- no
// roslib dependency for two message types.

// base_laser mount relative to base_link (mars.urdf base_laser_joint).
const LASER_OFFSET = { x: -0.0764, z: 0.17165 };

export class RosbridgePhysicsController {
  /** stampS is the driver's wall-clock publish time (header.stamp) -- same
   * physical clock as the browser's, so Date.now()/1000 - stampS measures
   * true delivery lag through rws/proxy (see SimSession's lag HUD).
   *
   * Pose and joints emit separately (one callback per source topic): merging
   * them into one stream stamps stale poses with fresh joint_states times,
   * which the interpolator then renders as stop-go motion. */
  onPose?: (pose: { x: number; y: number; z: number; yaw: number; stampS: number }) => void;
  onJoints?: (state: { joints: Record<string, number>; stampS: number }) => void;
  /** World-frame lidar hit points [x0,y0,z0, x1,...], null-range rays skipped. */
  onScan?: (points: Float32Array) => void;

  #url: string;
  #ws!: WebSocket;
  #open: Promise<void>;
  #resolveOpen!: () => void;
  #rejectOpen!: (err: Error) => void;
  #everOpened = false;
  #disposed = false;
  #retryMs = 500;
  #pose = { x: 0, y: 0, yaw: 0 };
  #joints: Record<string, number> = {};

  constructor(url: string) {
    this.#url = url;
    this.#open = new Promise((resolve, reject) => {
      this.#resolveOpen = resolve;
      this.#rejectOpen = reject;
    });
    this.#connect();
  }

  /** (Re)open the socket. A rosbridge session is stateless per connection, so
   * every open re-issues the subscriptions; on drop we retry with backoff
   * until dispose(), so a network blip doesn't kill the view. */
  #connect(): void {
    const ws = new WebSocket(this.#url);
    this.#ws = ws;
    ws.onopen = () => {
      this.#everOpened = true;
      this.#retryMs = 500;
      // No throttle on the state topics: rws throttling quantizes -- a 33ms
      // throttle on a 33ms-period topic drops every other sample (measured
      // 30Hz -> 15Hz with doubled jitter), which is exactly the rubber-band
      // feel in the 3D view. Full rate is ~40KB/s; the browser can take it.
      // queue_length 1: pose is state, not a stream -- any hop buffering more
      // than the newest sample converts load hiccups into permanent lag.
      this.#send({ op: "subscribe", topic: "/odom", type: "nav_msgs/msg/Odometry", queue_length: 1 });
      this.#send({ op: "subscribe", topic: "/joint_states", type: "sensor_msgs/msg/JointState", queue_length: 1 });
      this.#send({ op: "subscribe", topic: "/scan", type: "sensor_msgs/msg/LaserScan", throttle_rate: 150, queue_length: 1 });
      this.#resolveOpen();
    };
    ws.onerror = () => {
      // Settle init()'s await on a failed FIRST attempt (later errors are
      // no-ops on the settled promise); reconnection continues regardless.
      if (!this.#everOpened) this.#rejectOpen(new Error(`rosbridge connection failed: ${this.#url}`));
    };
    ws.onclose = () => {
      if (this.#disposed) return;
      setTimeout(() => this.#connect(), this.#retryMs);
      this.#retryMs = Math.min(this.#retryMs * 2, 5000);
    };
    ws.onmessage = (ev) => this.#onMessage(JSON.parse(ev.data as string));
  }

  async init(): Promise<void> {
    await this.#open;
  }

  dispose(): void {
    this.#disposed = true;
    this.#ws.close();
  }

  #onMessage(msg: { op: string; topic?: string; msg?: unknown }): void {
    if (msg.op !== "publish") return;
    if (msg.topic === "/odom") {
      const odom = msg.msg as {
        header: { stamp: { sec: number; nanosec: number } };
        pose: { pose: { position: { x: number; y: number }; orientation: { z: number; w: number } } };
      };
      const { position, orientation } = odom.pose.pose;
      this.#pose = { x: position.x, y: position.y, yaw: 2 * Math.atan2(orientation.z, orientation.w) };
      const stamp = odom.header.stamp;
      this.onPose?.({ ...this.#pose, z: 0, stampS: stamp.sec + stamp.nanosec * 1e-9 });
    } else if (msg.topic === "/joint_states") {
      const js = msg.msg as { header: { stamp: { sec: number; nanosec: number } }; name: string[]; position: number[] };
      js.name.forEach((name, i) => (this.#joints[name] = js.position[i]));
      // joint6M is the gripper's mirrored finger (URDF mimic of joint6,
      // multiplier -1) -- the driver doesn't publish it, so derive it here.
      this.#joints["joint6M"] = -(this.#joints["joint6"] ?? 0);
      const stamp = js.header.stamp;
      this.onJoints?.({ joints: { ...this.#joints }, stampS: stamp.sec + stamp.nanosec * 1e-9 });
    } else if (msg.topic === "/scan" && this.onScan) {
      const scan = msg.msg as { angle_min: number; angle_increment: number; range_max: number; ranges: number[] };
      const { x, y, yaw } = this.#pose;
      const cos = Math.cos(yaw);
      const sin = Math.sin(yaw);
      const originX = x + LASER_OFFSET.x * cos;
      const originY = y + LASER_OFFSET.x * sin;
      const points: number[] = [];
      scan.ranges.forEach((r, i) => {
        if (!Number.isFinite(r) || r <= 0 || r >= scan.range_max) return;
        const a = yaw + scan.angle_min + i * scan.angle_increment;
        points.push(originX + r * Math.cos(a), originY + r * Math.sin(a), LASER_OFFSET.z);
      });
      this.onScan(new Float32Array(points));
    }
  }

  #send(payload: object): void {
    if (this.#ws.readyState === WebSocket.OPEN) this.#ws.send(JSON.stringify(payload));
  }
}
