# sim-mujoco

Native MuJoCo simulation of the MARS robot in the apartment. Two things live
here:

1. **The virtual MARS driver** -- a headless sim that impersonates the
   robot's hardware drivers at the ROS topic level, so the full innate-os
   stack (brain_client, Nav2, AMCL, skills, webapp) runs against it
   unchanged. This is the sim backend `./innate sim up` launches.
2. **Asset pipeline + sandboxes** -- the offline tooling that generates the
   apartment collision/visual meshes, plus interactive viewers for physics
   debugging.

sim-web (the browser app) renders the same world: standalone with its own
MuJoCo WASM physics, or connected to this driver over rosbridge (open it
with `?ros`).

## Layout

```
virtual_mars_core.py   sim core: physics, servos, cameras, lidar, depth (no ROS)
virtual_mars_node.py   ROS 2 node exposing the real driver topic surface
run_virtual_mars.sh    in-container entry: robot_state_publisher + node
test_virtual_mars.py   headless check of the core (uv run, no ROS needed)

common.py              shared spawn pose + drive servo helpers
drive_apartment.py     world XML builder + scripted-tour viewer
drive_mars.py          model builders (URDF attach, planar base) + WASD sandbox
control_panel.html     browser control panel for drive_mars.py
stress_test_apartment.py  headless stability gate -- run after ANY collision
                          mesh or contact-parameter change

tools/                 asset pipeline (one-time generation, outputs in work/)
  split_apartment_obj.py     apartment OBJ -> per-room OBJs
  decompose_rooms.py         CoACD convex decomposition (collision hulls)
  bake_all_rooms.sh          decompose all rooms in parallel
  export_visual_rooms.py     textured room meshes for MuJoCo's renderer
  build_sdf_shells.py        watertight shells for the experimental SDF mode
```

## Setup

```bash
cd sim-mujoco
uv sync
uv run test_virtual_mars.py   # headless smoke test, saves camera frames to work/virtual_mars/
```

## The virtual MARS driver

`virtual_mars_node.py` publishes/subscribes the same topics, services, rates
and conventions as the real drivers (mars_bringup, the rplidar launch,
mars_arm, mars_cam) -- see its docstring for the full table. Highlights:

- `/odom` + TF odom->base_link (30Hz, pose-only like bringup.py); `/cmd_vel`
  in with the real 0.5s deadman.
- `/scan` (6Hz, 360 rays via mj_multiRay from the base_laser mount).
- Both cameras raw + JPEG-compressed (lazy -- rendered only while
  subscribed), tone-mapped to match sim-web's Three.js look.
- Depth image (16SC1 mm) + XYZ PointCloud2 + camera_info from MuJoCo's
  depth buffer -- no CUDA stereo estimator needed.
- `/mars/arm/commands` streaming, goto_js/goto_js_v2/goto_js_trajectory
  services (linear interpolation), head topics, `/joint_states`.
- `/virtual_mars/reset` (sim-only): respawn, used by sim-web's Reset.

Run `robot_state_publisher` with the same mars.urdf alongside it (the entry
script does) -- it supplies the static frames from /joint_states. AMCL owns
map->odom; the driver never publishes it.

The physics: apartment collision hulls + the real mars.urdf with a planar
(x/y/yaw) base -- a wheeled chassis can't pitch, and matching genesis'/the
webapp's planar convention avoids arm-reaction tip-over. Cameras mount on
the URDF's camera_optical_frame / arm_camera_link with genesis' FOV and
near plane.

### Running in the innate dev container

The sim image bakes the deps (libosmesa6 + mujoco/pillow/trimesh, see
sim/Dockerfile) and docker-compose.dev.yml mounts sim-mujoco/ + sim-web's
assets. `./innate sim up` starts the driver; to run it by hand:

    docker exec -it innate-dev /root/innate-os/sim-mujoco/run_virtual_mars.sh

Watch from Foxglove: Open connection -> Rosbridge -> ws://localhost:9090.
Useful panels: 3D (TF + /scan + /mars/main_camera/points), Image on the
compressed camera topics, Teleop publishing /cmd_vel.

Apartment meshes come from work/ (gitignored): on a fresh clone either
regenerate them (tools/, see below) or point VIRTUAL_MARS_ASSETS at a synced
copy.

## Asset pipeline

The apartment mesh is unwelded triangle soup, so collision geometry is
generated, not used raw:

```bash
cd tools
uv run split_apartment_obj.py        # per-room OBJs -> work/apartment_split/
./bake_all_rooms.sh                  # CoACD hulls -> work/apartment_split_v2/ (hours)
uv run export_visual_rooms.py        # textured rooms -> work/apartment_visual/
uv run --with scikit-image --with pymeshlab --with scipy build_sdf_shells.py  # optional, SDF mode
```

Publish to sim-web by copying the hulls flat into
`sim-web/public/physics/apartment_collisions_v2/` (+ regenerate its
manifest.json listing the OBJ filenames) and shells into
`sim-web/public/physics/apartment_sdf/`.

**After any regeneration, run `uv run stress_test_apartment.py` before
trusting the result** -- hull seams can corner-catch into divergence.

### Tuning notes (hard-won, don't rediscover)

- **CoACD preprocess_resolution** (tools/decompose_rooms.py): the rooms
  aren't watertight, so CoACD voxel-remeshes them first; the default
  resolution of 50 inflates every surface ~3.5cm (the "buffer" around
  furniture). 200 measures ~1.1cm median / 3.5cm max, 400 ~0.8cm / 2.6cm --
  maxima land at rounded furniture corners. Each doubling makes the bake
  several times slower; bake rooms in parallel.
- **Contact margin 0.007** (drive_apartment.py + sim-web's
  apartmentWorker.ts): the margin also bridges hull seams. At 3-5mm one
  seam corner-catches into a 20+ m/s single-step spike; 0.007 ran 5
  stress-tour loops clean. Don't lower it without re-gating.
- **implicitfast integrator**: what makes 1200+ hulls stable at all --
  damps single-step seam impulses that explode under Euler.
- **SDF shells** (experimental no-decomposition alternative): MuJoCo >= 3.3.5
  builds octree SDFs natively -- no hulls, no seams, ~2min bake. But the SDF
  sign is brutally sensitive to mesh topology: raw meshes and binary
  marching-cubes output (non-manifold corner junctions) both cause phantom
  deep-penetration impulses (robot flung 100+ m/s, ncon=0 either side).
  build_sdf_shells.py's gaussian-smoothed marching cubes +
  topology-preserving decimation is the recipe that survives a 5-loop
  stress tour. sim-web's "SDF collision (experimental)" toggle uses these.

## Sandboxes

- `uv run drive_mars.py` -- interactive viewer with the real URDF; drive
  from http://localhost:8766/control_panel.html (WASD/joystick over a local
  WebSocket -- macOS mjpython limitations rule out viewer-native keys).
- `uv run drive_apartment.py` -- scripted forward/turn tour with a
  placeholder box robot; press `3` in the viewer to toggle collision hulls
  vs the textured render.
