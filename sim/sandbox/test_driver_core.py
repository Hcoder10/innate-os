"""Headless check of mars_sim_driver.core (no ROS needed): settle, render
both cameras, drive via cmd_vel, verify motion and the stale-command
watchdog, then judge a challenge against the same world. Saves the rendered
frames to sim/assets/virtual_mars_test/ for eyeballing.

Usage: uv run sandbox/test_driver_core.py
"""

import math
import threading

import _driver_pkg  # noqa: F401
from mars_sim_driver import world
from mars_sim_driver.challenges import AllOf, Challenge, ChallengeEngine, Drop, Goal, Hold, Near, WorldState
from mars_sim_driver.core import VirtualMars, joint2_min_target

OUT_DIR = world.default_assets_dir() / "virtual_mars_test"


def main() -> None:
    sim = VirtualMars()
    sim.step(1.0)  # settle from spawn drop

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cam in ("main", "wrist"):
        jpeg = sim.render_jpeg(cam)
        assert jpeg[:2] == b"\xff\xd8", f"{cam}: not a JPEG"
        (OUT_DIR / f"{cam}.jpg").write_bytes(jpeg)
        print(f"{cam}: {len(jpeg)} byte JPEG -> {OUT_DIR / f'{cam}.jpg'}")

    x0, y0, yaw0 = sim.pose()
    for _ in range(20):  # re-send like a teleop publisher so the watchdog stays fed
        sim.set_cmd_vel(0.3, 0.0)
        sim.step(0.1)
    x1, y1, _ = sim.pose()
    moved = math.hypot(x1 - x0, y1 - y0)
    assert moved > 0.3, f"drove only {moved:.2f}m"
    print(f"cmd_vel drive: moved {moved:.2f}m")

    sim.step(1.0)  # no new commands -> watchdog stops the base
    x2, y2, _ = sim.pose()
    sim.step(0.5)
    x3, y3, _ = sim.pose()
    drift = math.hypot(x3 - x2, y3 - y2)
    assert drift < 0.05, f"base still moving {drift:.2f}m after cmd_vel went stale"
    print(f"watchdog: base stopped (drift {drift * 100:.1f}cm)")

    assert abs(sim.head_pitch_deg()) < 5.0
    print(f"head pitch: {sim.head_pitch_deg():.1f} deg")

    # Body guard: while joint1's target crosses the front arc, joint2 may not
    # stay folded past -0.5 (arm_control.cpp's floor) -- the arm ducks under
    # the head. Ramp math first, then the symptom that matters: a teleop-like
    # streamed joint1 sweep from home across the front must not jam.
    assert joint2_min_target(-1.4, -1.57) == -1.57  # outside the arc: full range
    assert joint2_min_target(0.0, -1.57) == -0.25  # front arc: duck
    assert abs(joint2_min_target(1.125, -1.57) - (-0.91)) < 1e-9  # mid-ramp
    assert joint2_min_target(1.25, -1.57) == -1.57
    home_j1 = world.ARM_HOME["joint1"]
    for leg_target in (-1.4, home_j1):  # across the front and back
        start = sim.joint_positions()["joint1"]
        for i in range(1, 41):  # ~0.07 rad per 100ms, teleop cadence
            sim.set_joint_target("joint1", start + (leg_target - start) * i / 40)
            sim.step(0.1)
        sim.step(3.0)
        p = sim.joint_positions()
        assert abs(p["joint1"] - leg_target) < 0.15, f"joint1 stalled at {p['joint1']:.2f} sweeping to {leg_target:.2f}"
    print(f"joint2 body guard: front-arc sweep ok both ways (j2 back at {p['joint2']:+.2f})")
    sim.reset()

    depth = sim.render_depth("main")
    center = float(depth[depth.shape[0] // 2, depth.shape[1] // 2])
    assert 0.1 < center < 15.0, f"implausible center depth {center}"
    print(f"depth: center pixel {center:.2f}m, range {depth.min():.2f}-{depth.max():.2f}m")

    scan = sim.lidar_scan(n_rays=360, max_range=12.0)
    hits = (scan < 12.0).sum()
    assert hits > 180, f"only {hits}/360 lidar rays hit indoors"
    assert scan.min() > 0.05, f"lidar sees something at {scan.min():.3f}m (inside the robot?)"
    print(f"lidar: {hits}/360 rays hit, nearest {scan.min():.2f}m, farthest hit {scan[scan < 12].max():.2f}m")

    check_challenges(sim)  # last: it moves the sim clock and leaves a prop out
    print("PASS")


def check_challenges(sim: VirtualMars) -> None:
    """The two ways the challenge judge (challenges.py) has been wrong.

    No GL and no rosbridge here -- predicates are pure functions of the world
    state, and the engine is fed by hand rather than by the physics thread."""
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=OUT_DIR / "challenges.json")
    engine.challenges["probe"] = Challenge(
        id="probe",
        title="Probe",
        brief="",
        # On the spawn point, where start()'s world reset puts the robot back:
        # goal 0 is true from the first tick that is allowed to judge. Goal 1
        # never is, so the run stays open for the assertions below.
        setup=[Drop("human", world.SPAWN_X, world.SPAWN_Y)],
        goals=[Goal("reach", Near("robot", "human", 1.5)), Goal("never", Near("robot", "nothing_here", 1.0))],
        time_limit_s=60.0,
    )

    # A tick landing between the world reset and the run being published must
    # not judge anything: it would measure the PREVIOUS run's world, and time
    # the new run from a started_t that is still whatever it was. On a server
    # up longer than the limit, that failed a run at birth. The physics thread
    # produces this by taking the sim lock between slices; a tick from inside
    # the drop is the same interleaving, deterministically.
    sim.data.time = 900.0
    mid_ticks = []
    real_reset, real_drop = sim.reset, sim.drop_prop_at

    def tick_now():
        mid_ticks.append(engine.tick(float(sim.data.time), sim.pose(), sim.object_centers()))

    def reset_and_tick():
        tick_now()  # before the reset: the OLD world, on the OLD clock (900s)
        real_reset()

    def drop_and_tick(*args, **kwargs):
        ok = real_drop(*args, **kwargs)
        tick_now()  # after the reset: a scene that is still being built
        return ok

    sim.reset, sim.drop_prop_at = reset_and_tick, drop_and_tick
    engine.start("probe")
    sim.reset, sim.drop_prop_at = real_reset, real_drop
    assert len(mid_ticks) >= 2, "the setup window was never ticked on both sides of the reset"
    assert all(tick["active"] is None for tick in mid_ticks), "judged a run that had not started"

    block = engine.tick(float(sim.data.time), sim.pose(), sim.object_centers())["active"]
    assert block["state"] == "running", f"fresh run born {block['state']} ({block['reason']})"
    assert block["elapsed_s"] == 0.0, f"fresh run started at {block['elapsed_s']}s"
    assert block["goals"][0]["done"], "judging never resumed after the setup window"
    print(f"challenge start: clean through {len(mid_ticks)} ticks in the setup window (900s uptime, 60s limit)")

    # AllOf must update every child, not stop at the first False: a Hold only
    # advances when it is asked, so short-circuiting restarts its dwell.
    dwell = Hold(Near("robot", "human", 1.5), seconds=5.0)
    both = AllOf([Near("robot", "nothing_here", 1.0), dwell])  # first child never true
    here = {"human": (0.0, 0.0)}
    for t in (0.0, 2.0, 6.0):
        both.update(WorldState(t=t, robot=(0.0, 0.0, 0.0), centers=here), [])
    assert dwell.update(WorldState(t=6.0, robot=(0.0, 0.0, 0.0), centers=here), []), "Hold behind AllOf lost its dwell"
    print("challenge predicates: Hold keeps its dwell behind a decided sibling")

    engine.abort()
    sim.remove_objects()
    sim.reset()


if __name__ == "__main__":
    main()
