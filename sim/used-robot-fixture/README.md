# Used-robot snapshot — 2026-08-08

A frozen snapshot of a robot (mars-the-3rd) after a realistic customer session on
release 96e8a51c, for testing upgrades/UX against a *used* robot rather than a
factory-fresh one.

## Restore

Check out this branch on the robot and run:

```bash
./scripts/restore_used_robot.sh
```

The script syncs the state below into place (deleting anything extra), restores
the large blobs, restarts `ros-app.service`, and prints a verification summary.
`VERIFY=1` additionally sha256-checks every blob.

## What the snapshot contains

**Repo (committed on this branch)**
- `ros2_ws/apt-dependencies.common.txt`: pins `ros-humble-zenoh-cpp-vendor`
  (prevents the rmw/vendor version split that bricked every node on 2026-08-08).
- `ros2_ws/src/brain/manipulation/src/recorder_main.cpp`: bounded-timeout
  MultiThreadedExecutor fix (recorder services previously hung forever).
- `ros2_ws/src/cloud/clients/training-manager/`: WIP training-manager client.

**User state (`state/`, synced by the restore script)**
- `workspace/custom_agents/`: `night_watch_agent.py`, `host_greeter_agent.py`.
- `workspace/custom_skills/`:
  - code skills `battery_check.py`, `look_around.py`, `patrol_waypoints.py`
  - `hello-salute/` replay skill (generated episode, runs on the arm)
  - `hiiiii/` replay skill (app-recorded)
  - `yr/` learned skill: 8 recorded episodes, encoded dataset, cloud training
    run output (10 ACT checkpoints + ONNX + TRT engine)
  - `fu/` learned skill still in training (no checkpoint)
  - deliberately-broken user artifacts, kept faithfully: `hello/` (replay
    missing `replay_file`), `wave/` (0-byte corrupt `metadata.json` next to a
    320 MB recording), empty drafts `h/`, `t/`
- `data/maps/office_demo.{yaml,pgm}` + `data/.last_mode` (`navigation`) +
  `data/.last_map` (`office_demo.yaml`)
- `~/patrol_waypoints.json` (one saved waypoint, `dock`)

**Large blobs (`blobs.manifest.tsv`, 16 files ≈ 3.8 GB)**
Files > 45 MB are not in git. The restore script fetches them automatically:
it prefers the local hardlink cache
(`/home/jetson1/robot-fixtures/used-robot-2026-08-08/blobs`, present on the
robot the snapshot was taken from) and otherwise downloads from the public
bucket `https://storage.googleapis.com/innate-robot-fixtures/used-robot-2026-08-08`
(sizes checked against the manifest; `VERIFY=1` adds sha256). Override either
with `BLOB_SOURCE=<dir-or-url>`. So on any robot the whole flow is just:

```bash
git checkout sim-used-robot-2026-08-08 && ./scripts/restore_used_robot.sh
```

The TRT engine blob is machine-specific; if it is missing or mismatched the
robot rebuilds it automatically on first policy load (~1–2 min).

## Not captured

- `.env` (holds `INNATE_SERVICE_KEY`; the script only warns if the key is absent)
- robot identity/calibration (`data/robot_info.json`, stereo calibration)
- OS/apt state beyond the dependency-list pin — install the release's packages
  via `post_update.sh` as usual
