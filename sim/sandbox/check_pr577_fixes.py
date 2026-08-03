"""Headless verification of the PR-577 review fixes.

Run from the repo root with the sim venv, no Docker and no ROS:

    VIRTUAL_MARS_ASSETS=<repo>/sim/assets \
    PYTHONPATH=ros2_ws/src/mars_bot/mars_sim_driver \
    sim/.venv/bin/python sim/sandbox/check_pr577_fixes.py

Each check prints what it measured, so a failure says which fix regressed
rather than just "assert False". Covers everything that does not need a
browser or a live goto; the settle timing and the visual fixes (per-mount
FOV, spec-driven props, shadows) are checked in the running sim.
"""

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

import mujoco
import numpy as np
from mars_sim_driver import constants, core, world, world_server
from mars_sim_driver.core import joint2_min_target

PASS, FAIL = "  ok  ", " FAIL "
failures = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"[{PASS if ok else FAIL}] {label}: {detail}")
    if not ok:
        failures.append(label)


# --- 1. camera FOV is derived from the focal length, not transcribed --------
fx_back = constants.CAMERA_HEIGHT / 2 / math.tan(math.radians(constants.CAMERA_FOVY) / 2)
check(
    "camera FOV derivation",
    abs(fx_back - constants.HEAD_CAMERA_FX) < 1e-9,
    f"CAMERA_FOVY={constants.CAMERA_FOVY:.4f}deg back-derives to fx={fx_back:.4f} (declared {constants.HEAD_CAMERA_FX})",
)
check(
    "wrist keeps its own lens",
    constants.WRIST_CAMERA_FOVY == 80,
    f"WRIST_CAMERA_FOVY={constants.WRIST_CAMERA_FOVY} (viewer's arm mount must match)",
)

# --- 2. model cache key covers the modules compiled INTO the model ---------
urdf = world.default_urdf_path()
probe = [urdf, Path(world.__file__), Path(constants.__file__), Path(core.__file__)]
before = core._model_cache_path("<xml/>", probe)
# Restore the timestamp afterwards: the point is that an edit moves the key,
# not to invalidate the real cache and make the last check recompile.
stamp = Path(constants.__file__).stat()
Path(constants.__file__).touch()
after = core._model_cache_path("<xml/>", probe)
os.utime(constants.__file__, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
restored = core._model_cache_path("<xml/>", probe)
check(
    "model cache keys constants.py",
    before != after and restored == before,
    f"touching constants.py moved the cache {before.name} -> {after.name}",
)

# --- 3. prop roster is structured, and MJCF + specs come from the one copy --
mjcf = world.grasp_object_bodies()
specs = world.grasp_object_specs()
check(
    "prop MJCF built from type/size",
    'type="box" size="0.02 0.02 0.02"' in mjcf and 'type="sphere" size="0.0225"' in mjcf,
    f"{len(world.GRASP_OBJECTS)} props emitted, cube/ball geometry as expected",
)
check(
    "object_specs roster complete",
    set(specs) == {o["name"] for o in world.GRASP_OBJECTS} and all(len(s["rgba"]) == 4 for s in specs.values()),
    f"specs={json.dumps(specs['can'])}",
)

# --- 4. joint2 guard matches the geometry that replaced the old boxes ------
spec = world.load_robot_spec(urdf)
world.add_planar_base(spec)
world.tune_contacts(spec)
model = spec.compile()
data = mujoco.MjData(model)


def base_link2_penetration(j1: float, j2: float) -> float:
    """Worst base_link<->link2 penetration (mm, <=0) at this arm pose."""
    for name, value in (
        ("joint1", j1),
        ("joint2", j2),
        ("joint3", 0.0),
        ("joint4", 0.0),
        ("joint5", 0.0),
        ("joint6", 0.0),
        ("joint_head", 0.0),
    ):
        data.qpos[model.jnt_qposadr[model.joint(name).id]] = value
    data.qpos[model.jnt_qposadr[model.joint("joint6M").id]] = 0.0
    mujoco.mj_forward(model, data)
    worst = 0.0
    for c in range(data.ncon):
        con = data.contact[c]
        bodies = {
            mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[con.geom1]),
            mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[con.geom2]),
        }
        if bodies == {"base_link", "link2"}:
            worst = min(worst, con.dist * 1000)
    return worst


arc = np.linspace(-1.0, 1.0, 21)
worst_at_guard = min(base_link2_penetration(float(j1), core.JOINT2_GUARD_MIN) for j1 in arc)
deeper = min(base_link2_penetration(float(j1), core.JOINT2_GUARD_MIN - 0.05) for j1 in arc)
check(
    "joint2 guard clears the new geometry",
    worst_at_guard == 0.0,
    f"guard={core.JOINT2_GUARD_MIN}: worst penetration {worst_at_guard:.2f}mm across 21 poses of the front arc",
)
check(
    "joint2 guard is not needlessly deep",
    deeper < 0.0,
    f"one 0.05 step past the guard ({core.JOINT2_GUARD_MIN - 0.05:.2f}) already bites at {deeper:.2f}mm",
)
check(
    "guard ramp agrees with the constant",
    joint2_min_target(0.0, -1.57) == core.JOINT2_GUARD_MIN and joint2_min_target(-1.4, -1.57) == -1.57,
    f"front arc -> {joint2_min_target(0.0, -1.57)}, outside the arc -> full range",
)

# --- 5. observer command channel survives junk without dying ---------------
server = object.__new__(world_server.WorldServer)
server.lock = threading.Lock()
ops = []
server.sim = types.SimpleNamespace(drop_objects=lambda: ops.append("drop"), remove_objects=lambda: ops.append("remove"))
print("       (the two 'stage command dropped' lines below are the point of this check)")
server._serve_scenario_commands(iter(["not json", '{"op":"drop_objects"}', "[1,2]", '{"op":"remove_objects"}']))
check(
    "junk frame does not kill the command channel",
    ops == ["drop", "remove"],
    f"both valid ops ran after two malformed frames: {ops}",
)

# --- 6. sync_robot_description is a true mirror ----------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "launcher"))
import runtime  # noqa: E402

tmp = Path(tempfile.mkdtemp())
try:
    pkg = tmp / "os" / "ros2_ws" / "src" / "mars_bot" / "mars_sim"
    (pkg / "urdf").mkdir(parents=True)
    (pkg / "meshes").mkdir()
    (pkg / "urdf" / "mars.urdf").write_text("<robot/>")
    (pkg / "meshes" / "link1.STL").write_bytes(b"FRESH")
    served = tmp / "sim" / "viewer" / "public" / "robot"
    (served / "meshes").mkdir(parents=True)
    (served / "meshes" / "link1.STL").write_bytes(b"STALE")
    (served / "meshes" / "renamed_away.STL").write_bytes(b"ORPHAN")
    cfg = {"os_repo": tmp / "os", "sim_repo": tmp / "sim"}
    runtime.sync_robot_description(cfg)
    refreshed = (served / "meshes" / "link1.STL").read_bytes() == b"FRESH"
    orphan_gone = not (served / "meshes" / "renamed_away.STL").exists()
    runtime.sync_robot_description(cfg)  # idempotent
    stable = (served / "meshes" / "link1.STL").read_bytes() == b"FRESH"
    check(
        "robot description mirrors (content compare + orphan removal)",
        refreshed and orphan_gone and stable,
        f"stale copy refreshed={refreshed}, orphan removed={orphan_gone}, second run stable={stable}",
    )
finally:
    shutil.rmtree(tmp)

# --- 7. live world: joint6M is streamed, props drop and park ---------------
print("       (building the world model -- a few minutes on a cold cache)")
sim = core.VirtualMars()
joints = sim.joint_positions()
check(
    "joint_positions streams the real joint6M",
    "joint6M" in joints,
    f"joint6M={joints.get('joint6M', float('nan')):+.4f} (viewer no longer has to mirror joint6)",
)
check("props start off-map", sim.object_poses() == {}, "object_poses() empty before a drop")
sim.drop_objects()
sim.step(1.0)
dropped = sim.object_poses()
on_floor = all(abs(p[2]) < 0.10 for p in dropped.values())
check(
    "drop_objects places every prop on the floor",
    set(dropped) == set(specs) and on_floor,
    f"{len(dropped)} props, heights {[round(p[2], 3) for p in dropped.values()]}",
)
sim.remove_objects()
check("remove_objects clears them", sim.object_poses() == {}, "object_poses() empty again")

print()
if failures:
    print(f"FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All headless checks passed.")
