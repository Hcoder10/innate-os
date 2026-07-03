# sim-web

Standalone, browser-only Three.js viewer for the apartment scene + MARS
robot. No ROS, no Python sim, no rosbridge — this is "Mode A": a kinematic
sandbox you can drive around with WASD or the on-screen joystick, running
entirely client-side.

Assets (URDF/meshes copied from `ros2_ws/src/mars_bot/mars_sim`, apartment
glb copied from `assets/appartement/source/appartement.glb`) live under
`public/` so Vite serves them as-is; the sources are left untouched.

## Run

```bash
npm install
npm run dev
```

Open the printed local URL (defaults to `http://localhost:5174`).

## Collision test sandbox

The apartment's MuJoCo collision geometry is a large triangle mesh
(`public/physics/world.xml` + `apartment_collision_*.obj`), which is much
harder to get right than simple primitives -- bad normals, non-manifold
geometry, or self-intersections there can make the robot spin wildly and
respawn instead of resting on the floor.

`test.html` is a completely separate, minimal entry point with its own
world (`public/physics/test_world.xml`) and its own worker
(`src/physics/testWorker.ts`): just a ground plane and two plain boxes
stacked on top of each other, no robot, no driving. It skips loading the
apartment glb/URDF and the drive/PD-servo logic entirely, so it starts
instantly and isolates whether odd physics behavior comes from the
apartment mesh itself or something more fundamental (timestep,
mass/inertia).

```bash
npm run dev
# open http://localhost:5174/test.html
```

Both boxes should settle and stay stacked without spinning or diverging.
Click and drag either cube to push it (same spring-force technique as
ufb-studio's drag interaction: `xfrc_applied` at the click point, cleared on
release). If this sandbox behaves but the apartment doesn't, the
apartment's collision mesh is the next thing to debug.

## Drive test sandbox

`drive-test.html` puts the actual driven robot (the free-floating box +
PD velocity-servo from `worker.ts`, same as the apartment) in
`public/physics/drive_test_world.xml`: a ground plane plus one fixed
obstacle cube, no apartment mesh. Drive it with WASD/joystick and ram it
into the obstacle -- it should push against it and stop cleanly.

```bash
npm run dev
# open http://localhost:5174/drive-test.html
```

Building this surfaced a real bug: `KP_FORWARD` (the forward-drive gain)
was only strong enough to produce ~10 N of force at max commanded speed,
but the box needs ~26.5 N to overcome static friction against the ground
(mass 3 kg, friction 0.9) -- so it never moved, just sat there. Raised to
200 (confirmed headlessly, no browser needed: `node` can load
`@mujoco/mujoco`'s WASM directly and step the model), which drives at a
steady ~0.3 m/s without the instability higher gains caused. `KP_YAW` was
left alone -- the geom's torsional/rolling friction (0.01 / 0.001) barely
resists yawing in place, so turning was never gated by friction the way
translation was, which is the likely reason the apartment's failure mode
looked like "stuck in place but spinning wildly" rather than just stuck.

## Layout

```
src/
  scene.ts                     Three.js scene: apartment glb + URDF robot, camera, render loop
  testScene.ts                  Three.js scene for the collision test sandbox (two box meshes)
  driveTestScene.ts              Three.js scene for the drive test sandbox (robot box + obstacle)
  robotPose.ts                  2D kinematic pose integrator (x, y, yaw)
  drive/
    curve.ts                    Joystick deadband/curve + velocity caps (ported from mars_control/app.cpp)
    driveController.ts           Arbitrates joystick vs. keyboard input
    keyboardDrive.ts             WASD/arrow-key drive + on-screen chip indicators (ported from webapp)
    joystick.ts                  On-screen SVG joystick (ported from webapp)
  physics/
    worker.ts                    MuJoCo WASM worker: robot drive (PD servo) against a given world XML
    physicsController.ts         Main-thread handle to worker.ts
    testWorker.ts                MuJoCo WASM worker for the collision sandbox (test_world.xml) + drag force
    testPhysicsController.ts     Main-thread handle to testWorker.ts
  main.ts                        Wires it all together for the apartment scene (index.html)
  test-main.ts                   Wires up the collision test sandbox (test.html)
  drive-test-main.ts              Wires up the drive test sandbox (drive-test.html)
public/
  robot/                         mars.urdf + STL meshes
  models/                        appartement.glb
  physics/
    apartment_collisions_v2/     1283-hull CoACD collision mesh + manifest.json (see below)
    world.xml                    apartment_collision_*.obj (112, old/stale) + test_world.xml + drive_test_world.xml (sandboxes)
```

Scene convention is Z-up, X-forward (matches ROS/REP-103), so the robot's
URDF loads with no axis remap; the apartment glb (authored Y-up, the glTF
convention) is rotated on load.

## Apartment collision geometry

The main apartment scene (`index.html` / `apartmentWorker.ts`) now loads the
real apartment collision mesh into the live physics model: the regenerated,
per-room CoACD decomposition from `sim-mujoco/` (1283 hulls at a 0.05m
concavity threshold; see that project's README for how they were generated,
why the old 112-piece `obj2mjcf` set was the "robot spins crazily" bug, and
the threshold/stability tradeoff that motivated switching to the
`implicitfast` integrator). The OBJs live under
`public/physics/apartment_collisions_v2/` (flat, room-prefixed filenames,
listed in `manifest.json` since a browser can't list a directory) and are
added to the mjSpec programmatically (`loadApartmentCollisions` in
`apartmentWorker.ts`) rather than through a hand-authored MJCF file, with the
same margin/friction/solref tuning and `implicitfast` integrator sim-mujoco
verified as stable. The HUD's "show collision mesh" checkbox
(`scene.ts`'s `loadCollisionDebug`) renders the same 1283-hull set as a
wireframe overlay for checking alignment against the visual glb.

`public/physics/world.xml` + the original 112 `apartment_collision_*.obj`
files are unrelated to this -- they only back the separate drive-test
sandbox below (`worker.ts`), which still has apartment collisions disabled
and hasn't been migrated to the new hull set.

## Not yet implemented

- Mode B: driving via rosbridge as an Innate-OS digital twin (subscribe
  `/cmd_vel`, publish `/odom`).
- Arm/head joints (kept at URDF defaults).
