# sim/viewer

The Three.js **SimSession library** the webapp embeds when the robot is
simulated: a live, full-resolution, drag-to-orbit 3D view of the apartment +
MARS robot, mirroring the `mars_sim_driver` container's state over rosbridge
(`/odom` + `/joint_states`, a few KB/s -- no video pipe). Physics never runs
in the browser; this is rendering only.

The standalone browser sim (MuJoCo WASM physics, no ROS) lives in the
separate `innate-sim-demo` repo -- it was extracted from this source tree
and keeps its own copy.

The stage has two sim-only debug chips: **lidar** (live `/scan` hit points)
and **collisions** (wireframe of the exact hull set the driver collides
against, for physics-vs-visual alignment checks).

The render assets (`public/models` glb, `public/robot` URDF+STLs,
`public/physics` hulls for the overlay) are not in git: `./innate-sim up`
extracts them from the published bundle (see `sim/sim-assets.lock`).

## Build

```bash
npm install
npm run build:lib   # dist-lib/sim-session.js, served by the webapp at /sim-viewer/
npm run typecheck
```

The webapp loads `/sim-viewer/sim-session.js` dynamically when `/robot_info`
reports `simulated: true`; real robots never request it (they use WebRTC),
and nothing here is ever installed on a robot.

## Layout

```
src/
  simSession.ts     Session facade (WebRtcSession-compatible state shape)
  simStage.ts       Mounts the canvas, render loop, PiP thumbnail blits
  scene.ts          Three.js scene: apartment glb + URDF robot, cameras
  physics/rosbridgeController.ts   /odom + /joint_states mirror (auto-reconnect)
public/             (fetched bundle) robot URDF+STLs, apartment glb
```

Scene convention is Z-up, X-forward (matches ROS/REP-103), so the robot's
URDF loads with no axis remap; the apartment glb (authored Y-up, the glTF
convention) is rotated on load.
