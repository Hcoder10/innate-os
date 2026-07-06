# Innate Local CLI

This directory holds the implementation of the local `innate` CLI. User-facing
configuration no longer lives here.

The local workflow uses:

- Python 3.10 or newer for the launcher
- the repo-root `.env` for secrets and cloud endpoint URLs
- `config/settings.yaml` for optional non-secret tunable ROS parameters and
  extra agent/skill dirs
- `sim/config.toml` for optional non-secret overrides (OS image, cloud-agent
  mode)

The CLI brings up:

- `innate-os` in its Docker-based simulation setup, including the virtual
  MARS driver (headless MuJoCo, `ros2_ws/.../mars_sim_driver`) as the sim backend --
  everything runs inside the container's ROS graph
- an optional local `innate-cloud-agent`

## Quick Start

```bash
cd innate-os
./innate-sim setup   # one-time: brain backend + service key into .env
./innate-sim up
```

`./innate-sim setup` configures the brain backend (hosted needs an Innate
service key, stored in `.env`). `./innate-sim up` starts the dev container,
builds/validates the ROS workspace, launches the tmux ROS session (including
the `sim-driver` window running the MuJoCo virtual robot), and waits until
`/odom` is publishing. On interactive terminals it then drops into a live
dashboard (`q` detaches, `Ctrl+C` stops the runtime).

## Everyday commands

```bash
./innate-sim status    # startup checks + health snapshot
./innate-sim logs      # startup logs; `logs os` / `logs agent` follow live
./innate-sim sh        # shell into the innate-dev container
./innate-sim down      # stop the runtime
./innate-sim clean     # remove containers/volumes (keeps .env + config)
```

Inside the container the ROS session lives in tmux: `tmux attach -t innate`
shows one window per subsystem (zenoh, rosbridge, sim-driver, nav-brain,
behavior, arm-ik, vision-nav, console-webapp).

## Viewing the simulation

- **webapp** -- `https://localhost`: in sim mode the camera panel is the
  SimSession 3D view (full-resolution Three.js driven by rosbridge state);
  teleop drives the sim robot exactly like a real one.
- **Foxglove** -- Open connection -> Rosbridge -> `ws://localhost:9090`.
  Panels for TF, `/scan`, `/mars/main_camera/points`, camera image topics,
  and `/cmd_vel` teleop.
- **Operator webapp** -- `https://localhost` (served by the container).
  Drive/arm controls work over rosbridge; camera video panels are not
  available in sim; the camera panel renders the sim directly (SimSession).
