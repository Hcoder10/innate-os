# Pickup Cube Smoke Test

This smoke test checks whether the current Genesis simulator stack can perform a
simple scripted cube pickup with Maurice's gripper. It uses the ROS Maurice URDF
from this repo, adds a rigid cube, records an MP4, and writes JSON/CSV metrics.

Run from the repo root:

```bash
sim/.venv/bin/python sim/scripts/pickup_trials/pickup_cube_smoke.py \
  --backend cpu \
  --name contact_pickup_6mm_cube_constraint002 \
  --out-dir sim/artifacts/pickup_cube \
  --cube-size 0.006 \
  --open-angle 0.85 \
  --close-angle 0.0 \
  --drive-mode position \
  --camera-pos 0.47 -0.22 0.16 \
  --camera-lookat 0.34 -0.05285 0.055 \
  --camera-fov 28
```

Useful invalid comparison:

```bash
sim/.venv/bin/python sim/scripts/pickup_trials/pickup_cube_smoke.py \
  --backend cpu \
  --name invalid_30mm_cube_contact_gated \
  --out-dir sim/artifacts/pickup_cube \
  --cube-size 0.030 \
  --open-angle 0.3491 \
  --close-angle 0.0 \
  --drive-mode position
```

Current finding: Genesis rigid contacts can lift and hold a 30 mm cube when the
arm/gripper are driven through `control_dofs_position`, but that is not a valid
contact pickup with the current Maurice gripper geometry: the fingers penetrate
the 30 mm cube deeply. The contact-gated smoke test fails that run.

The current contact-valid pickup uses a 6 mm cube, `--open-angle 0.85`, and
`--close-angle 0.0`. The harness requires gripper-cube contacts and rejects runs
whose max gripper-cube penetration exceeds `--max-allowed-penetration` (default
4 mm). Direct `set_dofs_position` is useful for kinematic debugging, but it does
not produce a stable grasp in this test.

The script exposes contact-related knobs:

- `--cube-friction` and `--robot-friction`
- `--noslip-iterations`
- `--solver-iterations`
- `--use-light-rigid-options`

The original URDF limit-derived gripper defaults are:

- `--open-angle 0.3491`
- `--close-angle 0.0`

For manipulation contact tests, use `--open-angle 0.85`, matching the gripper
open constant used by the manipulation interface.

The full simulator now defaults to manipulation-oriented contact settings when
`SIM_ENABLE_COLLISION` is true:

- `SIM_MANIPULATION_CONTACTS=1` uses multi-contact rigid options by default.
- `SIM_ARM_POSITION_CONTROL=1` drives arm joints through Genesis position
  controllers instead of teleporting them with direct setters.
- Set `SIM_LIGHT_RIGID_OPTIONS=1` or `SIM_ARM_POSITION_CONTROL=0` to restore the
  cheaper/debug behavior.
