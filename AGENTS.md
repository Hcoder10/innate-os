# Innate OS — Agent Reference

## What is Innate?

Innate OS is a lightweight, agentic operating system for the **MARS** — a small mobile robot with an arm.
It runs on an **NVIDIA Jetson Orin Nano 8GB** (resource-constrained by design) and boots the full stack in under a minute.

The OS is built on **ROS 2 (Humble)** with **Zenoh** as the DDS transport, and natively supports agentic workflows, vision-language navigation (VLN), and vision-language-action (VLA) models via an on-robot agent loop that calls a hosted vision-language model.

For full documentation and hardware details, see [innate.bot](https://www.innate.bot/) and [docs.innate.bot](https://docs.innate.bot).

## System Modes

| Mode | Description |
|---|---|
| `hardware` | Running on the physical MARS robot |
| `sim` | Running in the software simulator (same ROS nodes, mock sensors) |

## CLI — `innate`

Run `innate` with no arguments to print the current system status (version, mode, ROS/DDS state).

| Command | Description |
|---|---|
| `innate view` | Attach to running nodes |
| `innate restart` | Restart ROS nodes |
| `innate build` | Build and restart |
| `innate build release` | Build release mode and restart |
| `innate skill list` | List available robot skills |
| `innate update` | Check and apply updates |
| `innate diag` | Check hardware diagnostics |
| `innate volume` | Get or set speaker volume |
| `innate --help` | Show all commands |

## Robot Identity

A robot's identity — its service key, id, name and hardware facts — is **not baked into
the image**. A freshly flashed robot has no `/etc/innate.env` at all: `innate-seed.service`
gives it serial-derived defaults on first boot, and provisioning over BLE writes the real
one. That is what lets one generic image serve every robot.

### Where identity lives

Four places, lowest precedence first. [scripts/print_runtime_env.py](scripts/print_runtime_env.py)
merges the first three into the environment every ROS process sees:

| Layer | Holds | Written by |
|---|---|---|
| `/etc/innate_migrated.env` | facts *inferred* from derived state, never attested | `--migrate-info` |
| `/etc/innate.env` | the service key, `ROBOT_ID`, `ROBOT_NAME`, `MODULE_SERIAL` | `--seed`, `--write` |
| repo `.env` | developer overrides — **outranks the system file** | by hand |
| `data/robot_info.json` | what the running robot answers for name/id/revision | `app.cpp` |

Two of these catch people out, and both have caused real bugs:

- **The repo `.env` beats `/etc/innate.env`.** An R7.0 robot's key lives *only* there, so
  seeding must never fire on one. A stale `INNATE_SERVICE_KEY` left in it silently shadows
  a freshly provisioned key — which is why `--write` comments the old line out.
- **`robot_info.json` is sticky.** `app.cpp` fills a key only where it is missing or null,
  so a stored value outranks the environment from then on. The BLE layer reads it *before*
  the env too, so a de-provisioned robot keeps advertising its old name until it is dropped.

An unprovisioned robot names itself `MARS-<short-id>-unprovisioned`, with the short id four
hex chars `innate-identity` derives from the module serial — stable, so the same board
always comes back as the same name. `ROBOT_NAME` also drives the system
hostname through `sanitize_hostname`, so that robot answers at
`mars-<short-id>-unprovisioned.local`.

### `innate-identity`

Runs as root. The `--write` payload arrives on stdin, never argv — it carries the service
key and the login password, and argv is world-readable through `ps`.

| Mode | Does |
|---|---|
| `--seed` | serial-derived defaults if unprovisioned; run from `innate-seed.service` |
| `--write` | provision from a blob `{env, password, wipe_data?}`; refuses if already keyed |
| `--reset-info` | drop `robot_info.json` so `app.cpp` re-derives it |
| `--wipe-data` | clear maps, nav state, custom skills and agents, and `robot_info.json` |
| `--migrate-info` | record a fact that exists only in `robot_info.json` |
| `--unprovision` | **destructive**: delete the identity, reset the login password to factory |

`--write` and `--unprovision` are whole operations, not steps: each takes `ros-app` down,
re-derives or clears what the old identity left behind, and reboots. Every mode does its own
service juggling, so there is never a follow-up command to remember — and every mode is
`NOPASSWD` in `/etc/sudoers.d/innate-os` under both its repo path and
`/usr/local/bin/innate-identity`, so provisioning a robot is one line:

```bash
provision --emit | ssh robot 'sudo -n innate-identity --write'
```

The reboot it ends on is scheduled seconds out and detached, so the caller's answer — a BLE
notification, ssh's exit status and the `{robot_id, robot_name}` on stdout — is on the wire
before the link drops. Nothing else may reboot inline for the same reason.

`innate-seed.service` is pulled in by both `ros-app.service` and `ble-provisioner.service`
with `Wants=` + `After=` — never `Requires=`, because the seed exits 1 on anything with no
device-tree serial and must not fail a dev machine's boot.

A robot that has lost its service key cannot be re-keyed from the robot side. Ask for help.

## Writing Skills

Skills live in `workspace/` (see [workspace/README.md](workspace/README.md)). A skill is a
`Skill` subclass; everything it consumes is declared with a type annotation.

### Never `time.sleep` — always `self.sleep`

**In skill code, use `self.sleep(seconds)`. Never `time.sleep(seconds)`.**

`self.sleep` wakes and raises `SkillCancelled` the moment a Stop lands; `time.sleep` blocks
to completion, so a skill that uses it keeps running (and keeps the robot moving) after the
user pressed Stop. Sleeping is the only cancel point a loop needs — write the loop as if
cancel didn't exist and let the framework halt the base and report `CANCELLED`.

```python
while traveled < target:
    self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
    self.sleep(0.1)          # ✅ cancellable
    # time.sleep(0.1)        # ❌ Stop is ignored until the sleep finishes
```

`time` itself is fine for *measuring* — `time.time()` / `time.monotonic()` for deadlines and
elapsed checks. The rule is only about blocking.

Related cancel-aware helpers, all of which raise `SkillCancelled` too:

| Call | Use for |
|---|---|
| `self.sleep(seconds)` | Any pause in skill code |
| `self.wait_for(read, timeout)` | Block until a reader returns non-`None` |
| `self.check_cancelled()` | A checkpoint with no sleep (e.g. before an irreversible commit) |
| `self.cancelled` | Read the latch without raising |

Cleanup belongs in `try/finally` inside `execute()`. `self.on_cancel(hook)` is only for
forwarding a cancel to an external action goal — braking the base is automatic.

### The one exception: committed, non-cancellable sections

Teardown and already-committed physical actions must **not** be cancellable, so they use
`time.sleep` on purpose. Once `pick_any_object` closes the gripper, a cancel must not unwind
mid-grip and drop the object over the floor, so `_close_twist_lift` sleeps with `time.sleep`
and the run finishes carrying the object home.

If you write such a section, say so in a comment — otherwise the next reader "fixes" it back
to `self.sleep` and reintroduces the bug. Everywhere else, `self.sleep`.

## Key ROS Packages

| Package | Role |
|---|---|
| `mars_control` | Top-level app node, rosbridge websocket, teleop receiver |
| `mars_bringup` | Motor/IMU/LiDAR hardware drivers, TF tree |
| `mars_arm` | Arm + head servos, IK solver |
| `mars_cam` | Stereo cameras, depth estimation, WebRTC stream |
| `mars_nav` | Nav2 navigation, SLAM, mode manager |
| `brain_client` | Cloud brain bridge (STT/TTS, skills action server) |
| `manipulation` | Records/replays and runs manipulation policies |
