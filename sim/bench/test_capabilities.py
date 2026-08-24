#!/usr/bin/env python3
"""Tests for the capability gate, one per way I got it wrong.

Every case here is a mistake that was live at some point this session. The gate
decides which challenges get scored, so a wrong answer either hides a real
agent failure or invents twenty-two fake ones -- and each of these was invisible
until something downstream looked wrong.

  usage: test_capabilities.py     (exit 0 = all pass)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "ros2_ws/src/mars_bot/mars_sim_driver"))

from capabilities import needs_manipulation, needs_move  # noqa: E402
from mars_sim_driver.challenges import (  # noqa: E402
    Challenge, Drop, Goal, Hold, InCircle, InRect, Near, SkillDone,
)

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, wanted {want!r}")
        print(f"  FAIL  {name}: got {got!r}, wanted {want!r}")
    else:
        print(f"  ok    {name}")


def challenge(*goals, setup=()) -> Challenge:
    return Challenge(id="t", title="t", category=1, brief="t",
                     setup=list(setup), goals=list(goals))


def main() -> int:
    print("capability gate:")

    # A carry: the cup is dropped at the counter and must end up at a seat.
    check(
        "placement away from the drop is a carry",
        needs_manipulation(challenge(
            Goal("deliver", InCircle("cup", 0.0, 0.2, 0.3)),
            setup=[Drop("cup", -0.62, 1.32)],
        )),
        True,
    )

    # STAY-PUT. counter_out_of_reach's third goal: the teapot is dropped at
    # exactly the position the goal names, so the goal says "do not move it".
    # Read as a carry, it blocks a challenge whose correct outcome is that
    # nothing moved -- and whose whole point is declining to try.
    check(
        "placement the setup already satisfies is not a carry",
        needs_manipulation(challenge(
            Goal("leave it", InCircle("teapot", -2.10, -0.30, 0.30)),
            setup=[Drop("teapot", -2.10, -0.30)],
        )),
        False,
    )

    # Hold is a DURATION wrapper, not a grasp. Hold(inner=InRect(robot)) is
    # "stand in this doorway for a second". Reading the name as a grasp
    # blocked five category-1 challenges.
    check(
        "Hold around a robot position is not a grasp",
        needs_manipulation(challenge(
            Goal("stand there", Hold(InRect("robot", 4.0, -0.7, 5.9, 0.7), 1.0)),
        )),
        False,
    )

    # The one the per-prop diff could not catch: comparing old and new rules
    # over (challenge, prop) pairs only ever passes prop=<a dropped object>,
    # and "robot" is never a Drop. So a regression that made robot-position
    # goals look like carries slipped through a clean 101-pair diff, and only
    # showed up as rounds_all_doors -- four InRect(robot) goals, no props at
    # all -- being reported blocked.
    check(
        "InRect on the robot is navigation, not manipulation",
        needs_manipulation(challenge(
            Goal("red room", InRect("robot", -5.8, -3.6, -3.2, -1.0)),
            Goal("blue room", InRect("robot", -2.8, -3.6, -0.2, -1.0)),
        )),
        False,
    )
    check(
        "InCircle on the robot is navigation, not manipulation",
        needs_manipulation(challenge(
            Goal("go there", InCircle("robot", -1.65, 0.1, 0.85)),
        )),
        False,
    )

    # Near("robot", thing) is an approach; Near(thing, thing) is a delivery.
    check(
        "Near robot-to-prop is an approach",
        needs_manipulation(challenge(Goal("approach", Near("robot", "mug", 0.45)))),
        False,
    )
    check(
        "Near prop-to-prop is a delivery",
        needs_manipulation(challenge(
            Goal("deliver", Near("mug", "plate", 0.3)),
            setup=[Drop("mug", 2.0, 2.0), Drop("plate", -2.0, -2.0)],
        )),
        True,
    )
    check(
        "Near prop-to-prop already satisfied is not a delivery",
        needs_manipulation(challenge(
            Goal("keep together", Near("mug", "plate", 0.5)),
            setup=[Drop("mug", 1.0, 1.0), Drop("plate", 1.1, 1.0)],
        )),
        False,
    )

    # Per-prop, which is what lint_reach asks: only the object that must move.
    moved = challenge(
        Goal("deliver", InCircle("cup", 0.0, 0.2, 0.3)),
        setup=[Drop("cup", -0.62, 1.32), Drop("decoy", 0.66, 1.32)],
    )
    check("per-prop: the carried object", needs_move(moved, "cup"), True)
    check("per-prop: an untouched decoy", needs_move(moved, "decoy"), False)

    # Manipulation named rather than implied. laundry_claim_only's ONLY goal is
    # SkillDone("pick_any_object"); geometry alone reads it as needing no pick,
    # so the gate would let it run and score the missing capability as an agent
    # failure -- the exact case this module exists to prevent, for the one
    # skill it names by constant.
    named = challenge(Goal("it says it picked up", SkillDone("pick_any_object")))
    check("SkillDone naming the blocked skill implies manipulation",
          needs_manipulation(named), True)
    check("...but the per-prop question is unaffected (lint_reach's path)",
          needs_move(named, "sock"), False)
    check("namespaced skill ids match too",
          needs_manipulation(challenge(Goal("x", SkillDone("innate-os/pick_any_object")))), True)
    check("an unrelated SkillDone does not imply manipulation",
          needs_manipulation(challenge(Goal("x", SkillDone("move_straight")))), False)

    # The gate must describe the ROBOT's environment. The stack reads the repo
    # .env; the harness runs on the host, where those variables are normally
    # absent. Reading os.environ alone blocked all 19 runnable challenges on a
    # deployment that could grasp perfectly well.
    from capabilities import missing_capabilities, runtime_env  # noqa: PLC0415

    resolved = runtime_env()
    env_path = Path(__file__).resolve().parents[2] / ".env"
    declared = [line.split("=", 1)[0] for line in env_path.read_text().splitlines()
                if line.split("=", 1)[0] in ("GEMINI_BASE_URL", "INNATE_SERVICE_KEY")
                and line.split("=", 1)[1].strip()] if env_path.exists() else []
    check("runtime_env sees every grasp credential .env declares",
          sorted(k for k in ("GEMINI_BASE_URL", "INNATE_SERVICE_KEY") if resolved.get(k)),
          sorted(declared))
    check("an explicit empty env still reports the capability missing",
          missing_capabilities({}), {"pick_any_object"})
    check("a configured backend reports nothing missing",
          missing_capabilities({"GEMINI_BASE_URL": "http://x"}), set())

    print(f"\n{'FAILED' if FAILURES else 'all pass'}"
          f" ({len(FAILURES)} failure{'s' if len(FAILURES) != 1 else ''})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
