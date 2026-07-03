"""Interactive drive test against whatever apartment collision decompositions
exist so far under work/apartment_split_v2/<room>/<room>_collision_*.obj (see
split_apartment_obj.py + decompose_rooms.py). Run this yourself in a local
terminal -- it opens a native MuJoCo viewer window.

There's no live keyboard driving here: mujoco.viewer's interactive
key_callback only exists on launch_passive(), which on macOS refuses to run
unless the script itself is invoked via the `mjpython` launcher -- and that
launcher needs a "framework" Python build (the special
Python.framework/... layout from python.org's installer). uv's managed
CPython is a normal (non-framework) build, so `uv run mjpython ...` fails to
even dlopen its own interpreter. Rather than fight that, this script drives
the robot on a scripted forward/turn tour (same phases as
test_apartment_drive.py) via mujoco.set_mjcb_control, so you still get a
live view of the robot navigating the real collision meshes -- just not
WASD. It loops forever so you can rotate the camera and watch as long as
you like.

You can still interact manually with the mouse: double-click a body to
select it, then Ctrl + right-click-drag to shove it with a perturbation
force. Space bar pauses/resumes, and the left panel lets you scrub time and
inspect contacts.

Usage: uv run drive_apartment.py [work/apartment_split_v2]
"""

import math
import sys
from pathlib import Path

import mujoco
import mujoco.viewer

from common import KP_YAW, SPAWN_DROP_HEIGHT, SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG, apply_drive_force, launch_with_camera, set_spawn

MAX_LINEAR = 0.4
MAX_YAW = 1.0

# Chase-cam placement behind the spawned robot -- same numbers as scene.ts's
# spawnAt(), so the native viewer opens framed the same way sim-web does on
# spawn (rather than MuJoCo's own default free-camera framing, which -- for
# a spawn point close to a wall/corner -- can end up with the default
# azimuth/elevation looking straight into geometry from point-blank range).
CHASE_DISTANCE = 1.8  # meters behind the robot
CHASE_HEIGHT = 1.1  # meters above the ground
CHASE_TARGET_HEIGHT = 0.5  # meters -- camera looks at the robot's chest height


def spawn_camera_view(x: float, y: float, yaw_deg: float) -> tuple[float, float, float, float, float, float]:
    """(lookat_x, lookat_y, lookat_z, azimuth_deg, elevation_deg, extent) for
    MuJoCo's <statistic>/<visual><global> tags, converted from the same
    eye/lookat chase-cam placement scene.ts's spawnAt() uses."""
    yaw = math.radians(yaw_deg)
    forward_x, forward_y = math.cos(yaw), math.sin(yaw)
    eye = (x - forward_x * CHASE_DISTANCE, y - forward_y * CHASE_DISTANCE, CHASE_HEIGHT)
    lookat = (x, y, CHASE_TARGET_HEIGHT)

    eye_to_lookat = tuple(l - e for l, e in zip(lookat, eye))
    distance = math.sqrt(sum(c * c for c in eye_to_lookat))
    forward = tuple(c / distance for c in eye_to_lookat)  # unit vector, eye -> lookat

    # MuJoCo's free-camera convention (verified against mjv_cameraInModel):
    # forward = (cos(elevation)*cos(azimuth), cos(elevation)*sin(azimuth), sin(elevation)),
    # eye = lookat - distance*forward. Default camera distance = 1.5*stat.extent.
    elevation_deg = math.degrees(math.asin(forward[2]))
    azimuth_deg = math.degrees(math.atan2(forward[1], forward[0]))
    return lookat[0], lookat[1], lookat[2], azimuth_deg, elevation_deg, distance / 1.5


# (t_end, target_vx, target_wz) -- loops forever once it reaches the end.
TOUR = [
    (2.0, 0.0, 0.0),
    (8.0, MAX_LINEAR, 0.0),
    (11.0, 0.0, MAX_YAW),
    (17.0, MAX_LINEAR, 0.0),
    (20.0, 0.0, -MAX_YAW),
    (26.0, MAX_LINEAR, 0.0),
    (29.0, 0.0, MAX_YAW),
    (35.0, MAX_LINEAR, 0.0),
]

_PALETTE = [
    "0.9 0.2 0.2 1", "0.2 0.7 0.9 1", "0.9 0.7 0.1 1", "0.3 0.9 0.3 1",
    "0.8 0.3 0.9 1", "0.9 0.5 0.2 1", "0.2 0.9 0.7 1", "0.6 0.6 0.9 1",
]


# Robot geoms (both placeholder and real URDF) sit in the default group 0.
# Collision hulls and the render mesh get their own groups so MuJoCo's
# built-in group-visibility keys (0-5 in the viewer) toggle between them
# independently of the robot. Groups 3-5 are hidden by default (see
# mujoco.MjvOption()'s default geomgroup), groups 0-2 visible by default.
VISUAL_GROUP = 1
COLLISION_GROUP = 3


def find_decomposed_rooms(split_dir: Path) -> dict[str, list[Path]]:
    """{room_name: [collision piece paths]} for every room subdirectory that
    obj2mjcf --decompose has already produced under split_dir."""
    rooms: dict[str, list[Path]] = {}
    for room_dir in sorted(p for p in split_dir.iterdir() if p.is_dir()):
        pieces = sorted(room_dir.glob(f"{room_dir.name}_collision_*.obj"))
        if pieces:
            rooms[room_dir.name] = pieces
    return rooms


def find_visual_rooms(visual_dir: Path) -> dict[str, Path]:
    """{room_name: obj_path} for every room subdirectory export_visual_rooms.py
    has produced under visual_dir (each holds <room>.obj + <room>.png)."""
    rooms: dict[str, Path] = {}
    for room_dir in sorted(p for p in visual_dir.iterdir() if p.is_dir()):
        obj_path = room_dir / f"{room_dir.name}.obj"
        if obj_path.exists():
            rooms[room_dir.name] = obj_path
    return rooms


def build_world_xml(
    rooms: dict[str, list[Path]],
    include_placeholder_robot: bool = True,
    visual_rooms: dict[str, Path] | None = None,
) -> str:
    """The apartment environment (floor + decomposed room meshes). Pass
    include_placeholder_robot=False to get just the environment, e.g. to
    attach a real URDF robot via mjSpec instead (see drive_mars.py).

    visual_rooms, if given (see find_visual_rooms/export_visual_rooms.py),
    adds the textured render mesh sim-web shows -- in its own geom group,
    visible by default -- and pushes the (uglier, palette-colored) collision
    hulls into a group hidden by default, so the render is what you see on
    open and pressing '3' in the viewer toggles the collision hulls on for
    alignment checking. With no visual_rooms, collision hulls stay in the
    default-visible group as before."""
    collision_group = COLLISION_GROUP if visual_rooms else 0

    mesh_lines = []
    geom_lines = []
    i = 0
    for pieces in rooms.values():
        for piece in pieces:
            mesh_lines.append(f'    <mesh name="m{i}" file="{piece.resolve()}"/>')
            color = _PALETTE[i % len(_PALETTE)]
            # 7mm is the stress-tested floor: below it a hull seam corner-
            # catches (see README). Keep in sync with apartmentWorker.ts.
            geom_lines.append(
                f'      <geom name="{piece.stem}" mesh="m{i}" type="mesh" friction="0.9 0.01 0.001" '
                f'condim="6" margin="0.007" solref="0.02 1" rgba="{color}" group="{collision_group}"/>'
            )
            i += 1

    visual_mesh_lines = []
    visual_geom_lines = []
    for room, obj_path in (visual_rooms or {}).items():
        png_path = obj_path.with_suffix(".png")
        visual_mesh_lines.append(f'    <mesh name="vis_{room}" file="{obj_path.resolve()}"/>')
        visual_mesh_lines.append(f'    <texture name="tex_{room}" type="2d" file="{png_path.resolve()}"/>')
        visual_mesh_lines.append(f'    <material name="mat_{room}" texture="tex_{room}" specular="0" shininess="0"/>')
        visual_geom_lines.append(
            f'      <geom name="{room}_visual" mesh="vis_{room}" type="mesh" material="mat_{room}" '
            f'contype="0" conaffinity="0" group="{VISUAL_GROUP}"/>'
        )

    robot_body = (
        """
    <body name="robot_base" pos="0 0 0">
      <freejoint name="base_free"/>
      <inertial pos="-0.0587 0 0.0838" mass="3.0" diaginertia="0.03 0.03 0.02"/>
      <geom name="base_collision" type="box" size="0.0939 0.091 0.0838" pos="-0.0587 0 0.0838"
            friction="0.9 0.01 0.001" rgba="0.2 0.6 1 0.9"/>
    </body>"""
        if include_placeholder_robot
        else ""
    )

    lx, ly, lz, azimuth, elevation, extent = spawn_camera_view(SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG)

    return f"""
<mujoco model="apartment-drive-preview">
  <!-- implicitfast (vs. the default Euler) damps out the kind of single-step
       impulse spike a bad convex-hull-seam corner-catch produces, without
       needing a smaller timestep -- see README's "Collision mesh
       regeneration" for why the decomposed hulls can have such seams. -->
  <option timestep="0.002" gravity="0 0 -9.81" integrator="implicitfast"/>
  <!-- Without this, the viewer's default camera auto-frames the whole
       (large, many-room) apartment, so the robot -- spawned far from the
       apartment mesh's own centroid -- opens as a barely visible speck; and
       MuJoCo's own default azimuth/elevation can end up staring straight
       into a nearby wall from point-blank range. Frame the initial view the
       same way sim-web's spawnAt() chase-cam does instead. -->
  <visual>
    <global azimuth="{azimuth}" elevation="{elevation}" offwidth="1280" offheight="960"/>
  </visual>
  <statistic center="{lx} {ly} {lz}" extent="{extent}"/>
  <asset>
{chr(10).join(mesh_lines)}
{chr(10).join(visual_mesh_lines)}
  </asset>
  <worldbody>
    <!-- Same key + fill directional lights as scene.ts, at the same positions
         -- both are already defined in the sim's Z-up world frame. -->
    <light pos="4 -3 6" dir="-4 3 -6" diffuse="1 1 1"/>
    <light pos="-4 3 3" dir="4 -3 -3" diffuse="0.67 0.8 1"/>
    <geom name="ground" type="plane" size="20 20 0.1" friction="0.9 0.01 0.001" margin="0.007"
          solref="0.01 1" rgba="0.35 0.35 0.35 1" group="{collision_group}"/>
    <body name="apartment" quat="0.7071068 0.7071068 0 0">
{chr(10).join(geom_lines)}
{chr(10).join(visual_geom_lines)}
    </body>{robot_body}
  </worldbody>
</mujoco>
"""


def main() -> None:
    split_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "work" / "apartment_split_v2"
    rooms = find_decomposed_rooms(split_dir)
    if not rooms:
        raise SystemExit(f"no <room>/<room>_collision_*.obj found under {split_dir} -- run obj2mjcf --decompose first")

    total_pieces = sum(len(p) for p in rooms.values())
    print(f"Loaded decomposed rooms: {[(name, len(pieces)) for name, pieces in rooms.items()]} ({total_pieces} hulls total)")
    print("Not yet decomposed rooms won't have collision -- driving through their space is expected to fall through.")

    visual_dir = Path(__file__).resolve().parent / "work" / "apartment_visual"
    visual_rooms = find_visual_rooms(visual_dir) if visual_dir.is_dir() else {}
    if visual_rooms:
        print(f"Loaded {len(visual_rooms)} textured room(s) from {visual_dir} -- press '3' in the viewer to toggle collision hulls.")
    else:
        print(f"No textured rooms found under {visual_dir} -- run export_visual_rooms.py first for the sim-web-style render.")

    model = mujoco.MjModel.from_xml_string(build_world_xml(rooms, visual_rooms=visual_rooms))
    data = mujoco.MjData(model)
    set_spawn(model, data, SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG)
    mujoco.mj_forward(model, data)

    tour_length = TOUR[-1][0]

    def control_callback(model: mujoco.MjModel, data: mujoco.MjData) -> None:
        t = data.time % tour_length
        vx = wz = 0.0
        for t_end, phase_vx, phase_wz in TOUR:
            if t < t_end:
                vx, wz = phase_vx, phase_wz
                break
        apply_drive_force(model, data, vx, wz)

    mujoco.set_mjcb_control(control_callback)

    print(f"Scripted tour, loops every {tour_length:.0f}s. KP_YAW =", KP_YAW)
    print("Mouse: double-click a body then Ctrl+right-drag to push it. Space to pause.")
    lx, ly, lz, azimuth, elevation, extent = spawn_camera_view(SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG)
    launch_with_camera(model, data, (lx, ly, lz), azimuth, elevation, extent * 1.5)


if __name__ == "__main__":
    main()
