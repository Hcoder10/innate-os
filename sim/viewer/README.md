# sim/viewer

The Three.js **SimSession library** the webapp embeds when the robot is
simulated: a live, full-resolution, drag-to-orbit 3D view of the apartment +
MARS robot, fed by the world server's ground-truth observer stream (~75Hz
pose + joints over a WebSocket, a few KB/s -- no video pipe): direct to
loopback when the page is local, else the webapp's proxied `/worldstate`
route. Physics never runs in the browser; this is rendering only. Only the
lidar debug overlay reads a robot topic (`/scan` over rosbridge, connected
on first toggle), because it deliberately shows what the robot senses.

The standalone browser sim (MuJoCo WASM physics, no ROS) lives in the
separate `innate-sim-demo` repo -- it was extracted from this source tree
and keeps its own copy.

The stage has sim-only chips: **lidar** (live `/scan` hit points),
**collisions** (wireframe of everything the driver collides against, for
physics-vs-visual alignment checks: the apartment hull set plus the robot's
own `<collision>` primitives, which urdf-loader parses out of mars.urdf and
hangs off each link so they track the joints), and a **prop** row per set.

The apartment has no props in it by default -- every one of them starts parked
off-map. A prop chip puts one in the world, and the **drop | at robot** switch
says how: "at robot" sets it down at rest at its own tuned reach offset (drive
somewhere, lay a set out, practise grabbing), while "drop" takes over the
pointer so you click a spot and drag a heading, and the prop falls onto
whatever is under it. A set chip (`+manipulation`) lays out a whole group at
once. **clear** sends every prop back off-map, which is also what a sim reset
leaves behind.

`SimSession` also relays the world server's challenge judge
(`onChallenge`/`startChallenge`/`abortChallenge`, see
`mars_sim_driver/challenges.py`): the roster arrives once per connection, the
run state rides the stream, and the session merges the halves so a renderer
sees one view. The webapp's challenge panel is the only consumer, and it keys
off `onChallenge` existing to stay sim-only — nothing here judges anything.

The render assets (`public/models` glb, `public/physics` hulls) are not in
git: `./innate-sim up` extracts them from the published bundle (see
`sim/sim-assets.lock`). `public/robot` is different -- the URDF and STLs are
tracked source, and `up` refreshes the served copy from
`ros2_ws/src/mars_bot/mars_sim` so the browser can never draw a different
robot from the one the driver simulates.

## Build

Users never build this: `dist-lib/` ships prebuilt in the sim asset bundle
(`./innate-sim up` extracts it), so running the sim needs no Node.js. The
toolchain below is only for developing the viewer -- the launcher rebuilds
automatically when sources are newer than the bundle.

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
  simSession.ts     Session facade (WebRtcSession-compatible state shape),
                    jitter-sized interpolated playback of the state stream
  simStage.ts       Mounts the canvas, render loop (60fps cap), PiP thumbnails
  scene.ts          Three.js scene: apartment glb + URDF robot, cameras
  props.ts          Prop roster -> models the scene draws + stage buttons
  physics/worldStateController.ts  observer-stream client (auto-reconnect)
  physics/rosbridgeController.ts   /scan overlay source (lazy-connected)
public/             (fetched bundle) robot URDF+STLs, apartment glb
```

Scene convention is Z-up, X-forward (matches ROS/REP-103), so the robot's
URDF loads with no axis remap; the apartment glb (authored Y-up, the glTF
convention) is rotated on load.
