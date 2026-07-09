# Deep sleep via a custom nvpmodel mode (SLEEPNET) — experiment

Goal: a sleep state far below what light sleep (PR #512) can reach, that still
listens on the network for a wake signal. Reboots on entry/exit are accepted.

## Why a custom mode

Light sleep bottoms out around **6.3 W VDD_IN**: nvpmodel refuses to change
the online-CPU set on a live system (the 7W mode prompts for a reboot and
aborts), and even the 7W mode keeps EMC at 2133 MHz — VDD_SOC (~2.6 W) never
drops. SC7 suspend (~1 W) would be lower still, but the RTL8822CE's vendor
driver has no wake-on-WLAN, so a suspended robot is unreachable — a custom
*running* mode with the radio up is the lowest state that can hear the network.

## Prior art

Custom `nvpmodel.conf` modes are supported (NVIDIA's [Power Estimator
generates them](https://forums.developer.nvidia.com/t/how-to-create-a-custom-power-mode-configuration-for-a-orin-nano-super/350711)).
On the [lowest-power thread](https://forums.developer.nvidia.com/t/jetson-orin-nano-8gb-lowest-theoretical-operating-power-consumption/348662)
an Orin Nano 8GB reached **2.1–2.6 W VDD_IN** with a custom mode (2–4 cores at
low clocks, low GPU/EMC) *plus* device-tree disables (display/audio/ISP/PCIe),
SD-card boot and the WiFi module removed. [Another Super devkit
thread](https://forums.developer.nvidia.com/t/reducing-idle-power-on-orin-nano-super-dev-kit/358482)
got 4.7 → 2.5 W idle the same way. We keep WiFi (it *is* the wake path), NVMe
and the stock device tree, so the conf-only target here is **~3.5–4.5 W VDD_IN**
— roughly 2–3 W below light sleep, with tegrastats VDD_SOC finally moving.

## The SLEEPNET mode (ID=4)

Floors taken from this board's live tables (r36.4, Orin Nano Super):

| Knob | Stock 7W mode | SLEEPNET |
|---|---|---|
| CPU cores online | 4 | **2** |
| CPU clock | 729–960 MHz | **115–422 MHz** |
| GPU cap (rail auto-gates) | 408 MHz | **306 MHz** (lowest OPP) |
| EMC cap | 2133 MHz | **665.6 MHz** |
| TPC gating mask | 252 | 252 |

Further headroom if needed: try EMC below 665.6 MHz (bpmp clamps caps to its
rate table; the floor is untested here), and single-core operation.

## Rail gating too (the peripheral half)

The Jetson is only ~half the idle draw; the arm/head Dynamixels on the PCB's
HSSW rail are ~3-4 W of the rest. Because the whole ROS stack is down in deep
sleep, `bringup` isn't there to drive the PCB — so `pcb-power.py` talks to the
MCU (0x42) directly with the same `CMD_POWER` frame (firmware README §4.5) to
gate the rail on `enter` and restore it on `wake`. No wake-on-motion is armed
(the IMU sleeps too): deep sleep wakes over the network, so it doesn't need it.

Two ordering rules make this safe, both handled by `deep-sleep.sh`:
- **`enter` gates with the stack already stopped** — nothing else touches the
  I2C bus. The MCU holds the gate low across the Jetson reboot (firmware §4.5).
- **`wake` restores the rail *before* the reboot** — so when `mars_arm` boots
  it finds powered servos to initialize (it inits once at startup, no retry).

## Flow

```
sudo ./install.sh                       # add mode 4, install scripts + unit
sudo innate-deep-sleep enter            # stop ros-app, gate HSSW rail, arm wake
                                        # listener, reboot into SLEEPNET
                                        # → only network + sshd + listener run,
                                        #   arm/head/wheels unpowered
curl -X POST http://<robot>:4022/wake   # (or: sudo innate-deep-sleep wake)
                                        # restore rail, re-enable stack,
                                        # reboot into MAXN_SUPER
```

Mode persistence across the reboots is `/var/lib/nvpmodel/status`; which stack
comes up is plain systemd enablement; the rail gate is the MCU's own latched
state. Wake latency ≈ one full boot (~90 s). The listener is deliberately
unauthenticated for the experiment (waking is benign, LAN-only); add a token
before any wider rollout.

Recovery: if the rail doesn't come back after a wake, SSH in and
`sudo innate-deepsleep-pcb-power wake` (stack must be down), or restart the arm
node once the rail is up. Gating is best-effort — an I2C error on `enter` still
lets the Jetson deep-sleep (just without the rail cut).

## Validation checklist

- [ ] `sudo nvpmodel -p --verbose | grep -A16 SLEEPNET` parses with the values above
- [ ] `enter` reboots; after boot: `nproc` = 2, `nvpmodel -q` = SLEEPNET,
      policy0 max 422400, tegrastats VDD_SOC well under 2.5 W, VDD_IN recorded
- [ ] after `enter`: arm/head go limp and servo LEDs go dark (rail actually cut)
- [ ] `GET :4022/` answers; `POST :4022/wake` reboots to a fully working robot
      with the arm re-initialized and holding torque
- [ ] battery ammeter: SLEEPNET+gated vs SLEEPNET-only vs light sleep vs awake

## Follow-ups (out of scope)

- Wire `enter` into power_manager as a `/power/deep_sleep` service + app button
  (the app must then wake via `:4022/wake`, not rosbridge — the stack is down).
- Auth token on the listener; mDNS advertisement so the app can find the robot.
- Let the wake listener also poll the MCU motion latch (0x83 status byte) so a
  physical nudge can wake deep sleep too, not only the network.
