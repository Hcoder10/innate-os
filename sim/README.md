# innate sim

One-command development environment: the full innate-os robot stack (brain,
Nav2, skills, webapp) running in Docker against a headless MuJoCo simulation
of the MARS robot in an apartment. Local code edits + `innate build`
(inside the container) behave exactly like on a real robot.

```bash
./innate-sim setup     # one-time: keys + config
./innate-sim up        # container + ROS stack + virtual robot
```

## Architecture

- **Robot stack** -- the real innate-os ROS 2 graph, unchanged, in the
  `innate-dev` container (see `docker-compose.dev.yml`), launched as tmux
  windows by `scripts/launch_sim_in_tmux.zsh`.
- **Virtual MARS driver** (`ros2_ws/src/mars_bot/mars_sim_driver/`) -- a
  headless MuJoCo ROS 2 package that impersonates the hardware drivers at
  the topic level: odom/TF, lidar `/scan`, both cameras (raw + JPEG), depth
  + point cloud, arm/head commands and services. Launched as
  `ros2 launch mars_sim_driver sim_driver.launch.py` (tmux window
  `sim-driver`). Readiness = `/odom` publishing.
- **Viewing** -- the operator webapp at `https://localhost` IS the sim UI:
  in sim mode its camera panel is a SimSession (built from `viewer/`) that
  renders the live simulation with Three.js from rosbridge state -- main
  and wrist robot-camera views plus a sim-only orbit chase view, all at
  render rate, no video streaming. (The standalone browser sim with WASM
  physics lives in the separate innate-sim-demo repo.)
- **Dev tooling** -- `sandbox/` (native MuJoCo viewers + physics stress
  gates, see its README), `tools/` (apartment asset pipeline -> `assets/`,
  gitignored), `viewer/` (the Three.js renderer package).
- **Launcher** (`launcher/`) -- the `./innate-sim` CLI: container lifecycle,
  ROS workspace builds, brain backend config, status dashboard.

## The simulation, layer by layer

The sim is a stack of three layers inside
`ros2_ws/src/mars_bot/mars_sim_driver/`; each is usable without the ones
above it:

```
node.py    mars_sim_driver -- ROS 2 node impersonating the hardware drivers
core.py    VirtualMars     -- the simulation itself (physics + sensors), no ROS
world.py   model building  -- MJCF world + URDF robot, pure functions
```

### world.py -- the model

Builds the MuJoCo model: apartment collision hulls + textured visual rooms
(from `sim/assets`, see below), the real `mars.urdf` attached on a planar
base (x/y/yaw -- a wheeled robot can't pitch), drive gains and contact
parameters. Pure functions over files; `sim/sandbox` imports it for the
native viewers, and a future GPU/batched backend (e.g. MuJoCo Warp) would
consume the same spec.

### core.py -- VirtualMars (headless, no ROS)

The whole simulated world in one Python object. No ROS dependency: use it
in a script, notebook, pytest, or RL loop -- and instantiate several in one
process for parallel rollouts. The API is shaped like a robot:

```python
from mars_sim_driver.core import VirtualMars

sim = VirtualMars()
sim.step(1.0)                          # settle from spawn; step(dt) runs physics
sim.set_cmd_vel(0.3, 0.5)              # vx m/s, wz rad/s (0.5s watchdog)
sim.set_joint_target("joint2", -1.0)   # arm/head PD servo setpoints
x, y, yaw = sim.pose()                 # ground truth
rgb   = sim.render_rgb("main")         # 640x480 ("wrist" = arm camera)
depth = sim.render_depth("main")       # meters; robot's own geoms excluded
scan  = sim.lidar_scan(360, 12.0)      # planar lidar off the visual surfaces
grid, ox, oy = sim.occupancy_grid()    # rasterized nav map (-1/0/100)
sim.reset()                            # back to spawn, arm home
```

Start with the walkthrough notebook: `sim/sandbox/virtual_mars_demo.ipynb`
(`cd sim && uv sync --group notebook`, then open it on the `sim/.venv`
kernel). `update_camera()`/`read_rgb()` are split (same for depth) so
callers can update the scene under a lock but render outside it -- that's
what keeps physics from stalling in the driver.

### node.py -- mars_sim_driver (the digital twin's hardware)

The ROS wrapper that makes VirtualMars *be* the robot. Design rule:
impersonate the hardware drivers exactly -- same topics, types, rates and
frame names -- so everything above (Nav2, AMCL, brain, webapp, Foxglove)
runs unmodified. The full topic/service surface with rates is in the
`node.py` module docstring; highlights:

- `/odom` + TF @30Hz, `/scan` @6Hz, cameras @7.5/5Hz JPEG, depth + point
  cloud @8Hz with the real stereo pipeline's [0.25, 2.0]m clamp
- arm/head command topics and `goto_js*` services
- latched `/robot_info` `{"simulated": true}` -- how the webapp knows to
  render the Three.js view instead of opening WebRTC
- `/virtual_mars/reset` (sim-only)

Physics runs on a steady clock under a lock; a dedicated render thread does
the expensive OSMesa camera renders so a slow render can never freeze the
robot. Camera topics render lazily -- no subscribers, no GL work -- which is
what makes headless runs cheap.

`sim_driver.launch.py` also starts `robot_state_publisher` (same URDF as the
real bringup) and `grid_localizer_sim`, the stand-in for the CUDA-only
grid_localizer: identical lifecycle/service contract, but it seeds AMCL from
ground truth (republishing until AMCL confirms with `/amcl_pose`).

## Configuration

`config.toml` (created from `config.toml.template` by setup): OS image
selection and cloud-agent mode. Secrets live in the repo-root `.env`
(`INNATE_SERVICE_KEY`, brain backend settings) -- `./innate-sim setup`
walks through them.

## Day-to-day

```bash
./innate-sim up          # start (or resume) everything
./innate-sim status      # dashboard
./innate-sim logs        # startup logs; `logs os` / `logs agent` for live ones
./innate-sim sh          # shell into the container
./innate-sim sh          # then `innate build` to rebuild ros2_ws after code changes
./innate-sim down        # stop
```

The world itself (physics + sensor rendering) always runs in the **world
server** (`mars_sim_driver/world_server.py`); the driver node is a thin RPC
client, so there is exactly one render path. Only the server's *placement*
varies: on macOS `up` runs it natively on the host (Docker has no GPU there
-- host GL renders ~7x faster, full resolution), elsewhere/in CI the launch
file starts the same server inside the container (software GL, scaled
renders, identical wire contract). `INNATE_SIM_HOST_WORLD=1/0` forces host
placement on/off; without `uv` on the host it falls back to in-container
with a warning. `./innate-sim logs world-server` shows the host server log.

The generated geometry (driver meshes in `sim/assets/`, the viewer's hulls,
GLB, and robot meshes) is not in git: `./innate-sim up` downloads the bundle
pinned by `sim/sim-assets.lock` from a GitHub release and extracts it in
place (one-time, ~100 MB). To change the geometry, run the pipeline in
`sim/tools/` (see sim/sandbox/README.md), then
`uv run tools/publish_assets.py --publish` to publish + repin the lock.
The viewer's SimSession bundle is built with `cd sim/viewer && npm run
build:lib`.
