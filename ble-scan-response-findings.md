# BLE names never appear — root causes, 2026-08-14

Two independent bugs, either of which alone hides robot names. Both are proven with HCI
captures. A third section covers the tool that works around both today.

## Bug 1 (robot): the name is never transmitted

`btmon` on mars-the-blue across `systemctl restart ble-provisioner`:

- bluetoothd → kernel: MGMT `Add Extended Advertising Parameters (0x0054)` +
  `Add Extended Advertising Data (0x0055)` carrying **`Scan response length: 15`,
  `Name (complete): mars-the-blue`**
- kernel → controller: `LE Set Extended Advertising Data (0x0037)` ×2, `Read Local Name`,
  `LE Set Extended Advertising Enable (0x0039)`
- **`LE Set Extended Scan Response Data (0x0038)`: never issued** (count 0, twice over)

The set is created correctly — `Properties: 0x0013` (Connectable, **Scannable**, legacy
ADV_IND) — and bluetoothd sets `MGMT_ADV_PARAM_SCAN_RSP` (BIT(16), shown by btmon as
`Unknown advertising flag (0x00010000)`), so neither known upstream bug applies:

- [Bluetooth: Fix not sending Set Extended Scan Response](https://lkml.iu.edu/hypermail/linux/kernel/2107.1/03990.html) (`a76a0d365077`, in 5.10+)
- [Bluetooth: Allow scannable adv with extended MGMT APIs](https://lkml.iu.edu/hypermail/linux/kernel/2103.0/06424.html)

### It is path-dependent

A *second* advertisement registered while the provisioner's instance is already advertising
(bluezero, `Includes = {'local-name'}`, no `LocalName`) is programmed correctly:

| | provisioner's instance 1 | added instance 2 |
|---|---|---|
| MGMT flags | `0x00010043` — Add Local Name in Scan Response | same |
| `0x0038` issued | **no** | **yes, Status Success** |
| scan-response content | — | `Name (short): mars-the-b` |
| set properties | Connectable, Scannable, ADV_IND | identical |

Applying `Includes = {'local-name'}` to the provisioner itself does **not** help — the flag
reaches the kernel and `0x0038` still never fires. That patch was deployed to mars-the-blue,
verified as ineffective, and reverted; both the robot and this checkout are back to original.

Environment: kernel `5.15.148-tegra`, BlueZ 5.64, bluezero, RTL8822CE combo (BT over USB
`13d3:3549`, manufacturer 93). Advertising is otherwise healthy: legacy ADV_IND, 1M PHY,
ch 37/38/39, TX 0 dBm, interval 1280 ms.

**Still unverified:** whether the second-instance trick actually puts a name *on air*. It
needs a receiver that can see scan responses, and the only laptop here cannot (Bug 2). The
clean test is robot-to-robot: patch one robot, scan from another.

## Bug 2 (laptop km-ubuntu): scan responses are never received, from anything

Five `btmon` captures on the laptop's Intel controller (`manufacturer 2`, HCI version 12 /
BT 5.3), each during an active discovery:

| condition | scan-response reports (`0x001a`/`0x001b`) |
|---|---|
| default | 0 |
| after `systemctl restart bluetooth` | 0 |
| `Filter duplicates: Disabled` (bluetoothctl `duplicate-data on`) | 0 |
| `quirk_strict_duplicate_filter = N` | 0 |
| scan confirmed `Type: Active (0x01)` in all cases | — |

The same room, at the same time, seen by mars-the-blue's Realtek receiver in one 18 s scan:
**247 `0x001b` scan-response reports and 72 names.**

So this laptop drops every scan response, from every device — not just the robots. Any
device whose name rides in the scan response reads as unnamed here. Names that *do* appear
locally (`LE_WH-1000XM4`, `Quest 3`, `OsmoNano-8D53`) carry their name in the ADV packet.

Legacy scanning can't be used as a workaround: `hcitool lescan` fails with
`Set scan parameters failed: Input/output error` even with bluetoothd stopped and after an
HCI reset, because the host has already used the extended scan API.

Likely a firmware or kernel bug for this Intel part; the next step is a `linux-firmware`
and kernel update, then re-run the capture and count `0x001b` events.

### Related laptop symptom

bluetoothd frequently exposes devices with `RSSI = None` and no properties while `btmgmt`
reports them at −58 dBm. Also, an open GNOME Bluetooth Settings panel holds a permanent
discovery session, which makes `btmgmt find` fail with `status 0x0a (Busy)` and starves
other scanners. Close that panel when scanning.

## What works today: `find-robot.py`

In the repo root. Matches robots on the service UUID (never on the name — nothing airs one),
then recovers each name by connecting and reading GAP `0x2A00`, exactly as
`innate-controller-app/src/services/BleConnectionService.ts` does:

```
$ ./find-robot.py
scanning 15s ...
9C:C7:D3:F6:BD:3E  rssi= -55  mars-the-blue
9C:C7:D3:97:AE:DA  rssi=   ?  MARS-750B
```

`--fast` skips name resolution. Connects to a 1280 ms advertiser lose the race often, so the
tool retries once; an occasional `(name unavailable)` still happens.

This also explains why the controller app needs its GATT `0x2A00` read and learned-name
cache: that workaround is load-bearing, because the name never rides the air on any
platform. It also puts a question mark on the `BleService.ts` comment claiming "iOS merges
the scan response into localName reliably" — with nothing transmitted, that cannot have been
observed; those names came from GATT or cache.

## Considered and measured: publishing two packets for backward compatibility

The idea: keep today's advertisement untouched for existing clients, and add a second
advertising instance carrying the name, so old and new clients both work. Two measurements
say it does not work on this stack as-is.

1. **`LocalName` can never be placed in the ADV packet.** BlueZ routes it to the scan
   response unconditionally. Registering the second instance as `broadcast` still produced
   `Properties: 0x0013` (Connectable, Scannable) with `Name (complete): mars-the-blue` in the
   scan response — straight back into Bug 1. A second packet would have to carry the name as
   ServiceData or ManufacturerData, which old clients ignore (so compatibility holds in that
   direction) but which requires an app-side parse.
2. **Only one instance ever gets airtime.** With three instances registered
   (`ActiveInstances: 0x03`), every report across a full `btmgmt find` sweep was
   `eir_len 25` — instance 1 alone. A newly registered instance airs briefly right after
   registration (the `eir_len 21` seen when instance 2 was added) and then stops. No
   rotation appears in a 12 s HCI capture either.

So the compatible shape has to be a **single** 31-byte ADV carrying flags + identity + a
name field, not two rotating packets. Within the budget that means dropping `Appearance`
(4 B) and either accepting an 8-char shortened name alongside the 128-bit UUID, or moving to
a 16-bit UUID (frees 16 B) and carrying the full name. Both need matching app changes.

## Fix options

**Robot (Bug 1)**
1. Newer kernel than 5.15.148-tegra — the advertising sync path has changed substantially.
2. Verify and ship the second-instance workaround (register a name-only instance after the
   main one). Needs the robot-to-robot on-air test first.
3. Put the identity in the ADV packet. Budget is 31 B: flags(3) + 128-bit UUID(18) +
   appearance(4) = 25. Dropping appearance frees 4 B → an 8-char shortened name. A 16-bit
   service UUID frees 16 B and fits the full name, but both need matching app changes —
   Android's offloaded scan filter matches the ADV packet only.

**Laptop (Bug 2)**
1. Update `linux-firmware` + kernel, then re-count `0x001b` in a capture.
2. Until then, use `find-robot.py`, or scan from a robot (`ssh <robot> bluetoothctl scan le`).

## Secondary finding

Advertising interval is 1280 ms (BlueZ default; bluezero exposes no knob). Not a correctness
bug, but ~1.3 s per beacon per robot makes discovery and connection setup slow, and it is why
name resolution needs a retry.
