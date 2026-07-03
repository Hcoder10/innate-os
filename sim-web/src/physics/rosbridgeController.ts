// Connected-mode ("Mode B") physics source: instead of stepping MuJoCo WASM
// locally, subscribe to the real innate-os ROS graph over rosbridge and
// mirror the virtual MARS driver's state (see sim-mujoco/virtual_mars_node.py).
// Same public surface as ApartmentPhysicsController so main.ts can swap them.
//
// Speaks the rosbridge JSON protocol directly (subscribe/advertise/publish) --
// no roslib dependency for four message types.

import type { JointInfo, Pose2D } from "./apartmentPhysicsController";

const ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"] as const;
const CMD_VEL_HZ = 15;
const ARM_COMMAND_HZ = 10;

// base_laser mount relative to base_link (mars.urdf base_laser_joint).
const LASER_OFFSET = { x: -0.0764, z: 0.17165 };

export class RosbridgePhysicsController {
  onPose?: (pose: { x: number; y: number; z: number; yaw: number; joints: Record<string, number> }) => void;
  /** World-frame lidar hit points [x0,y0,z0, x1,...], null-range rays skipped. */
  onScan?: (points: Float32Array) => void;
  /** data: image/jpeg URI of the latest frame from the selected camera feed. */
  onFrame?: (dataUri: string) => void;

  #feedTopic: string | null = null;

  #ws: WebSocket;
  #open: Promise<void>;
  #pose = { x: 0, y: 0, yaw: 0 };
  #joints: Record<string, number> = {};
  #lastCmdVel = 0;
  #lastArmCommand = 0;
  #lastArmSent: number[] = [];
  #lastHeadSentDeg = Number.NaN;

  constructor(url: string) {
    this.#ws = new WebSocket(url);
    this.#open = new Promise((resolve, reject) => {
      this.#ws.onopen = () => resolve();
      this.#ws.onerror = () => reject(new Error(`rosbridge connection failed: ${url}`));
    });
    this.#ws.onmessage = (ev) => this.#onMessage(JSON.parse(ev.data as string));
  }

  async init(_spawn: Pose2D): Promise<{ joints: JointInfo[] }> {
    await this.#open;
    this.#send({ op: "subscribe", topic: "/odom", type: "nav_msgs/msg/Odometry", throttle_rate: 33 });
    this.#send({ op: "subscribe", topic: "/joint_states", type: "sensor_msgs/msg/JointState", throttle_rate: 33 });
    this.#send({ op: "subscribe", topic: "/scan", type: "sensor_msgs/msg/LaserScan", throttle_rate: 150 });
    this.#send({ op: "advertise", topic: "/cmd_vel", type: "geometry_msgs/msg/Twist" });
    this.#send({ op: "advertise", topic: "/mars/arm/commands", type: "std_msgs/msg/Float64MultiArray" });
    this.#send({ op: "advertise", topic: "/mars/head/set_position", type: "std_msgs/msg/Int32" });
    this.#send({ op: "advertise", topic: "/virtual_mars/reset", type: "std_msgs/msg/Empty" });

    // Joint slider limits come from the same URDF the scene renders.
    const urdf = new DOMParser().parseFromString(await (await fetch("/robot/mars.urdf")).text(), "text/xml");
    const joints: JointInfo[] = [];
    for (const name of [...ARM_JOINTS, "joint_head"]) {
      const el = [...urdf.querySelectorAll("joint")].find((j) => j.getAttribute("name") === name);
      const limit = el?.querySelector("limit");
      joints.push({
        name,
        lower: parseFloat(limit?.getAttribute("lower") ?? "-3.14"),
        upper: parseFloat(limit?.getAttribute("upper") ?? "3.14"),
      });
    }
    return { joints };
  }

  reset(_spawn: Pose2D): void {
    this.#send({ op: "publish", topic: "/virtual_mars/reset", msg: {} });
  }

  /** Subscribe to a CompressedImage topic (or null to stop). Unsubscribing
   * also stops the driver's rendering for that camera (lazy topics). */
  setCameraFeed(topic: string | null): void {
    if (this.#feedTopic === topic) return;
    if (this.#feedTopic) this.#send({ op: "unsubscribe", topic: this.#feedTopic });
    this.#feedTopic = topic;
    if (topic) {
      this.#send({ op: "subscribe", topic, type: "sensor_msgs/msg/CompressedImage", throttle_rate: 133 });
    }
  }

  step(_dt: number, vx: number, wz: number, joints: Record<string, number>): void {
    const now = performance.now();
    if (now - this.#lastCmdVel > 1000 / CMD_VEL_HZ) {
      this.#lastCmdVel = now;
      this.#send({
        op: "publish",
        topic: "/cmd_vel",
        msg: { linear: { x: vx, y: 0, z: 0 }, angular: { x: 0, y: 0, z: wz } },
      });
    }
    if (now - this.#lastArmCommand > 1000 / ARM_COMMAND_HZ) {
      this.#lastArmCommand = now;
      const arm = ARM_JOINTS.map((n) => joints[n] ?? 0);
      if (arm.some((v, i) => v !== this.#lastArmSent[i])) {
        this.#lastArmSent = arm;
        this.#send({
          op: "publish",
          topic: "/mars/arm/commands",
          msg: { layout: { dim: [], data_offset: 0 }, data: arm },
        });
      }
      const headDeg = Math.round(((joints["joint_head"] ?? 0) * 180) / Math.PI);
      if (headDeg !== this.#lastHeadSentDeg) {
        this.#lastHeadSentDeg = headDeg;
        this.#send({ op: "publish", topic: "/mars/head/set_position", msg: { data: headDeg } });
      }
    }
  }

  setGains(_kp: number, _kd: number, _effortLimit: number): void {} // gains live in the driver

  applyForce(_anchor: [number, number, number], _force: [number, number, number]): void {}
  clearForce(): void {}

  dispose(): void {
    this.#ws.close();
  }

  #onMessage(msg: { op: string; topic?: string; msg?: unknown }): void {
    if (msg.op !== "publish") return;
    if (msg.topic === "/odom") {
      const odom = msg.msg as { pose: { pose: { position: { x: number; y: number }; orientation: { z: number; w: number } } } };
      const { position, orientation } = odom.pose.pose;
      this.#pose = { x: position.x, y: position.y, yaw: 2 * Math.atan2(orientation.z, orientation.w) };
      this.#emit();
    } else if (msg.topic === "/joint_states") {
      const js = msg.msg as { name: string[]; position: number[] };
      js.name.forEach((name, i) => (this.#joints[name] = js.position[i]));
      // joint6M mirrors joint6 (see urdfWorker.ts) -- the driver doesn't
      // publish it, so derive it for the visual.
      this.#joints["joint6M"] = -(this.#joints["joint6"] ?? 0);
      this.#emit();
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
    } else if (msg.topic === this.#feedTopic && this.onFrame) {
      const frame = msg.msg as { data: string };  // rws base64-encodes uint8[]
      this.onFrame(`data:image/jpeg;base64,${frame.data}`);
    }
  }

  #emit(): void {
    this.onPose?.({ x: this.#pose.x, y: this.#pose.y, z: 0, yaw: this.#pose.yaw, joints: this.#joints });
  }

  #send(payload: object): void {
    if (this.#ws.readyState === WebSocket.OPEN) this.#ws.send(JSON.stringify(payload));
  }
}
