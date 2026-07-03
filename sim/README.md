# innate sim

One-command development environment: the full innate-os robot stack (brain,
Nav2, skills, webapp) running in Docker against a headless MuJoCo simulation
of the MARS robot in an apartment. Local code edits + `./innate build`
behave exactly like on a real robot.

```bash
./innate sim setup     # one-time: keys + config
./innate sim up        # container + ROS stack + virtual robot
```

## Architecture

- **Robot stack** -- the real innate-os ROS 2 graph, unchanged, in the
  `innate-dev` container (see `docker-compose.dev.yml`), launched as tmux
  windows by `scripts/launch_sim_in_tmux.zsh`.
- **Virtual MARS driver** (`sim-mujoco/`) -- a headless MuJoCo node that
  impersonates the hardware drivers at the ROS topic level: odom/TF, lidar
  `/scan`, both cameras (raw + JPEG), depth + point cloud, arm/head commands
  and services. Runs inside the container (tmux window `sim-driver`) so it
  shares the zenoh graph. Readiness = `/odom` publishing.
- **Viewing** -- sim-web in connected mode (`sim-web/`, open with `?ros`)
  renders the simulation in the browser at native resolution from rosbridge
  state (`ws://localhost:9090`); Foxglove connects to the same port for
  debug panels (TF, `/scan`, point cloud, camera images, teleop). No video
  streaming, no compression.
- **Launcher** (`launcher/`) -- the `./innate sim` CLI: container lifecycle,
  ROS workspace builds, brain backend config, status dashboard.

The previous genesis-based simulator (host process, WebRTC video, `:8000`
HTTP API) has been removed; see git history if you need to dig it up.

## Configuration

`config.toml` (created from `config.toml.template` by setup): OS image
selection and cloud-agent mode. Secrets live in the repo-root `.env`
(`INNATE_SERVICE_KEY`, brain backend settings) -- `./innate sim setup`
walks through them.

## Day-to-day

```bash
./innate sim up          # start (or resume) everything
./innate sim status      # dashboard
./innate sim logs        # startup logs; `logs os` / `logs agent` for live ones
./innate sim sh          # shell into the container
./innate build           # rebuild ros2_ws after code changes
./innate sim down        # stop
```

The apartment collision/visual meshes the driver loads come from
`sim-mujoco/work/` (gitignored). If they're missing (fresh clone), generate
them with the pipeline in `sim-mujoco/tools/` (see sim-mujoco/README.md) or
set `VIRTUAL_MARS_ASSETS` to a synced copy.
