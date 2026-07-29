# Known issue: silent odometry freeze (I2C read path wedges)

*Observed live on R7-27, 2026-07-26. Recovered by `innate restart`. Root cause
narrowed but not pinned to one side of the bus. Written up for follow-up.*

## Symptom

- Robot drives normally under teleop/nav (commands reach the motors).
- The robot's position on the map does not move. AMCL never corrects.
- `/odom` keeps publishing at a healthy 30 Hz with fresh timestamps — but the
  pose inside is frozen (in this incident: exactly `x=0, y=0` with a constant
  yaw, i.e. the value latched at the moment the freeze began).
- Nothing appears in the logs. No node crashes.

Quick field check: `ros2 topic echo /odom --field pose.pose.position` twice a
few seconds apart while driving. Identical values while the robot moves =
this issue. Recovery: `innate restart` (re-initializes both ends of the I2C
transaction).

## How the odometry pipeline works

`bringup.py` → `i2c.py` (`I2CManager`) runs a ~30 Hz loop; each cycle is a
two-step I2C transaction with the drivetrain MCU (ItsyBitsy M4 at `0x42`):

1. **write** 8 bytes: `CMD_MOVE` (speed, turn) — always sent, every cycle
2. **read** 8 bytes: `RESP_MOVE` (x cm, y cm, theta·100 as int16) + CRC-8

The received pose is stored in a **latched** `current_transform`
(`i2c.py::_process_response`). Independently, a 30 Hz timer
(`bringup.py::_publish_odometry`) publishes whatever the latch holds, stamping
it with **the current time on every publish** (deliberate — downstream TF
consumers need fresh stamps).

## What happened

The **read** half of the transaction stopped yielding valid responses while
the **write** half kept working. Consequences cascade:

| stage | effect |
|---|---|
| I2C reads fail / return garbage | `_read_response()` returns `None`; latch never updates |
| latch stale | `/odom` publishes a frozen pose — at full rate, with fresh stamps |
| odom reports no motion | AMCL gates scan-matching on odometry motion → never corrects |
| map pose frozen | the robot's dot sits still while the physical robot drives |

Which end wedged is unconfirmed: candidates are the Jetson-side smbus read
state, or the MCU's Arduino `Wire` slave response handshake
(`main-firmware.ino::requestEvent` serves a `response_ready` buffer prepared
after each command — the Wire slave state machine is a known fragile spot).
A restart re-initializes both ends, which fixed it without attributing blame.
If it recurs, the discriminating test is watching the MCU's own USB serial
console (it prints every command/response) while the freeze is active.

## Why it was hard to diagnose (design smells)

1. **Staleness is invisible by construction.** Re-stamping the latched pose
   with `now()` means consumers see a healthy-looking 30 Hz odometry stream
   containing dead data. No timeout can fire anywhere downstream; "unchanged
   because stationary" and "unchanged because wedged" are indistinguishable.
2. **The failure is silent.** Read failures and CRC mismatches in
   `i2c.py::_read_response` are logged at **debug** level only — production
   logs show nothing.
3. **A confusable twin symptom exists**: the MCU integrates *commanded* wheel
   speeds for x/y (not encoder feedback — see `mars-firmware`
   `MotorControl.cpp::updatePosition`), so a robot pushed *by hand* also
   doesn't move on the map. Same symptom, unrelated cause. (Heading is
   IMU-based and does track hand-rotation, which distinguishes the two.)

## Proposed hardening (small, not yet implemented)

In `I2CManager` (`ros2_ws/src/mars_bot/mars_bringup/mars_bringup/i2c.py`):

- Count consecutive cycles without a valid `RESP_MOVE`.
- After ~1 s (≈30 misses): log a **warning** (rate-limited), and either stop
  publishing `/odom` or mark it degraded. A missing odometry stream fails
  loudly everywhere downstream; a frozen one fails silently. Optionally also
  attempt a re-init of the smbus handle before giving up.

Firmware side (optional, when next flashing): make `requestEvent` always
serve the latest pose rather than a one-shot `response_ready` flag, removing
the handshake state that can desync.

## Related context

- The wider zig-zag investigation notes (odometry quantization, open-loop
  x/y, the IMU yaw-rate fix branch `mars-firmware@yaw-rate-loop`) are in the
  session memory and `zigzag-explainer.html`.
- Recovery cheat-sheet entry: **"robot drives but map frozen + `/odom` pose
  constant → `innate restart`."**
