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


def call(path: str, iface: str, method: str, args: GLib.Variant | None = None) -> GLib.Variant:
    return bus.call_sync("org.bluez", path, iface, method, args, None, Gio.DBusCallFlags.NONE, -1, None)


def managed_objects() -> dict:
    return call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")[0]


def prop(path: str, iface: str, name: str):
    return call(path, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (iface, name)))[0]


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


def connect(device_path: str) -> bool:
    """Connecting to a 1280 ms advertiser routinely loses the first race; one retry wins it."""
    for attempt in range(2):
        try:
            call(device_path, "org.bluez.Device1", "Connect")
            return True
        except GLib.Error:
            if attempt == 0:
                wait_until(lambda: False, 2)
    return False


def resolve_name(device_path: str) -> str | None:
    """Connect just long enough to learn the robot's name, then let it go.

    BlueZ usually fills in Name from the GAP read during connection setup; the explicit
    characteristic read is the fallback for when it does not.
    """
    if not connect(device_path):
        return None
    try:
        wait_until(lambda: prop(device_path, "org.bluez.Device1", "ServicesResolved"), CONNECT_TIMEOUT_S)
        name = prop(device_path, "org.bluez.Device1", "Name")
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
    if not robot["name"] and "--fast" not in sys.argv:
        robot["name"] = resolve_name(path)
    rssi = robot["rssi"] if robot["rssi"] is not None else "?"
    print(f"{robot['address']}  rssi={rssi:>4}  {robot['name'] or '(name unavailable)'}")
