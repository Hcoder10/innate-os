#!/usr/bin/env python3
"""Find Innate robots over BLE and print name / address / RSSI.

Matches on the provisioning service UUID, never on the name: the robots do not put
their name on air at all (the kernel never issues LE Set Extended Scan Response Data
— see ble-scan-response-findings.md), so a name filter would match nothing. Names are
recovered the way the controller app does it, by connecting and reading GAP 0x2A00.

    ./find-robot.py            # list every robot, resolving names
    ./find-robot.py --fast     # skip name resolution
"""

import sys

import gi

gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
ADAPTER = "/org/bluez/hci0"
SCAN_S = 15
CONNECT_TIMEOUT_S = 12

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def call(
    path: str, iface: str, method: str, args: GLib.Variant | None = None, timeout_ms: int = -1
) -> GLib.Variant:
    return bus.call_sync("org.bluez", path, iface, method, args, None, Gio.DBusCallFlags.NONE, timeout_ms, None)


def managed_objects() -> dict:
    return call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")[0]


def prop(path: str, iface: str, name: str):
    return call(path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (iface, name)))[0]


def try_prop(path: str, iface: str, name: str):
    """A property BlueZ has not learned yet is absent, not empty."""
    try:
        return prop(path, iface, name)
    except GLib.Error:
        return None


def wait_until(predicate, timeout_s: int):
    """Spin the main loop until predicate() is truthy, or the timeout expires."""
    loop = GLib.MainLoop()
    state = {"value": None}

    def poll():
        state["value"] = predicate()
        if state["value"]:
            loop.quit()
            return False
        return True

    GLib.timeout_add(300, poll)
    GLib.timeout_add_seconds(timeout_s, lambda: (loop.quit(), False)[1])
    loop.run()
    return state["value"]


def gatt_device_name(device_path: str) -> str | None:
    """The Device Name characteristic under this device, once services are resolved."""
    for path, ifaces in managed_objects().items():
        chrc = ifaces.get("org.bluez.GattCharacteristic1")
        if not path.startswith(device_path + "/") or not chrc:
            continue
        if chrc.get("UUID", "").lower() != DEVICE_NAME_UUID:
            continue
        value = call(path, "org.bluez.GattCharacteristic1", "ReadValue", GLib.Variant("(a{sv})", ({},)))[0]
        return bytes(value).decode("utf-8", "replace").strip() or None
    return None


def resolve_name(device_path: str) -> str | None:
    """Connect just long enough to learn the robot's name, then let it go.

    A Connect that outlives the D-Bus reply still lands, so the error is ignored and the
    name polled for regardless. ServicesResolved never turns true against these robots,
    so it cannot be the thing waited on; BlueZ fills in Name from the GAP read within a
    second or two of the link coming up, and the characteristic read is the fallback.
    """
    try:
        call(device_path, "org.bluez.Device1", "Connect", timeout_ms=CONNECT_TIMEOUT_S * 1000)
    except GLib.Error:
        pass
    try:
        name = wait_until(lambda: try_prop(device_path, "org.bluez.Device1", "Name"), CONNECT_TIMEOUT_S)
        return name or gatt_device_name(device_path)
    except GLib.Error:
        return None
    finally:
        try:
            call(device_path, "org.bluez.Device1", "Disconnect")
        except GLib.Error:
            pass


def scan() -> dict[str, dict]:
    """Robots seen in one discovery window, keyed by object path."""
    call(
        ADAPTER,
        "org.bluez.Adapter1",
        "SetDiscoveryFilter",
        GLib.Variant(
            "(a{sv})",
            (
                {
                    "Transport": GLib.Variant("s", "le"),
                    "UUIDs": GLib.Variant("as", [SERVICE_UUID]),
                    "DuplicateData": GLib.Variant("b", False),
                },
            ),
        ),
    )
    call(ADAPTER, "org.bluez.Adapter1", "StartDiscovery")
    print(f"scanning {SCAN_S}s ...", file=sys.stderr)

    found: dict[str, dict] = {}

    def collect():
        for path, ifaces in managed_objects().items():
            props = ifaces.get("org.bluez.Device1")
            if not props or SERVICE_UUID not in [u.lower() for u in props.get("UUIDs") or []]:
                continue
            seen = found.setdefault(path, {"address": props.get("Address"), "rssi": None, "name": None})
            seen["rssi"] = props.get("RSSI") or seen["rssi"]
            seen["name"] = props.get("Name") or seen["name"]
        return True

    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(1, collect)
    GLib.timeout_add_seconds(SCAN_S, lambda: (loop.quit(), False)[1])
    loop.run()
    collect()
    call(ADAPTER, "org.bluez.Adapter1", "StopDiscovery")
    return found


robots = scan()
if not robots:
    print("no Innate robots found", file=sys.stderr)
    raise SystemExit(1)

for path, robot in sorted(robots.items(), key=lambda kv: -(kv[1]["rssi"] or -999)):
    for _ in range(2 if "--fast" not in sys.argv else 0):
        if robot["name"]:
            break
        robot["name"] = resolve_name(path)
    rssi = robot["rssi"] if robot["rssi"] is not None else "?"
    print(f"{robot['address']}  rssi={rssi:>4}  {robot['name'] or '(name unavailable)'}")
