"""Simplified Python agents for the benchmark, per Innate's note that
VirtualMars can drive one directly instead of the full innate-os brain.

An agent is anything with act(mars, t) -> None called every control tick. It
drives the base with set_cmd_vel and may use place_prop_at_robot to model a
carry. CMD_VEL_TIMEOUT_S is 0.5, so velocity must be RE-SENT every tick like a
teleop publisher -- an agent that commands once and then thinks stops moving.
"""

from __future__ import annotations

import math

# Base limits. Kept conservative: the point of the oracle is to prove a
# challenge is solvable, not to set a fast time.
V_MAX = 0.30
W_MAX = 1.2
# Arrival slop is part of every goal radius: the agent stops up to ARRIVE_M
# short of a waypoint, so an approach point 0.45 m from a target can leave the
# robot 0.63 m away. At 0.18 that silently failed four challenges whose goal
# radius was 0.5.
ARRIVE_M = 0.07
FACE_RAD = 0.25
# How close "near" gets to a prop. Inside every fetch challenge's goal radius
# (0.45-0.55) but outside the base's own footprint, so the approach ends beside
# the object rather than through it.
NEAR_STANDOFF = 0.34


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class RandomAgent:
    """The validity gate. ARC-AGI-3 screens every environment against a random
    policy, because a task a random policy can pass measures nothing. This one
    re-rolls a velocity every `hold_s` so it actually explores rather than
    jittering in place -- a weaker random baseline would make the gate too easy
    to pass and hide a trivial challenge.
    """

    name = "random"

    def __init__(self, seed: int = 0, hold_s: float = 1.2):
        import random

        self.rng = random.Random(seed)
        self.hold_s = hold_s
        self._until = -1.0
        self._cmd = (0.0, 0.0)

    def reset(self, mars, challenge) -> None:
        self._until = -1.0

    def act(self, mars, t: float) -> None:
        if t >= self._until:
            self._cmd = (self.rng.uniform(-0.1, V_MAX), self.rng.uniform(-W_MAX, W_MAX))
            self._until = t + self.hold_s
        mars.set_cmd_vel(*self._cmd)


class ScriptedAgent:
    """The solvability oracle: a waypoint follower with a modelled carry.

    This is NOT a claim about the real agent's ability. It exists so a failing
    benchmark result can be attributed: if the oracle cannot finish a challenge
    either, the challenge is broken (unreachable goal, wrong-signed coordinate,
    a door too narrow for the base) and no result from it means anything.

    A carried prop is NOT dragged along behind the robot. The first version
    re-placed it at its `reach` offset every tick, which put a solid body 0.6 m
    directly ahead of a robot driving forward: it collided, got shoved, was
    teleported back, and the base stalled against it for the whole time limit.
    Three of four fetch challenges failed that way with the object reached and
    the delivery never made.

    Instead "grab" only remembers the prop and "put" teleports it to the
    destination. Nothing is claimed about the arm either way -- these plans
    validate goal logic and navigable geometry, and a carry that cannot collide
    with its own carrier is the abstraction that keeps that honest.
    """

    name = "oracle"

    def __init__(self, plan: list[tuple]):
        self.plan = plan
        self.i = 0
        self.carrying: str | None = None
        self._t0: float | None = None

    def reset(self, mars, challenge) -> None:
        self.i = 0
        self.carrying = None
        self._t0 = None

    @property
    def done(self) -> bool:
        return self.i >= len(self.plan)

    def act(self, mars, t: float) -> None:
        if self.done:
            mars.set_cmd_vel(0.0, 0.0)
            return

        step = self.plan[self.i]
        op = step[0]

        if op == "goto":
            _, tx, ty = step
            if self._drive_to(mars, tx, ty):
                self._advance()
        elif op == "near":
            # Approach a prop by name and stop at a STANDOFF, not on top of it.
            # Driving to the prop's own centre meant colliding with it, shoving
            # it, and then chasing the target it had just displaced -- the three
            # fetch challenges reached their object and never advanced past it.
            _, prop = step
            p = mars.object_centers().get(prop)
            if p is None:
                self._advance()
                return
            x, y, _yaw = mars.pose()
            if math.hypot(p[0] - x, p[1] - y) <= NEAR_STANDOFF:
                mars.set_cmd_vel(0.0, 0.0)
                self._advance()
            else:
                self._drive_to(mars, p[0], p[1])
        elif op == "grab":
            self.carrying = step[1]
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "release":
            self.carrying = None
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "put":
            # Models a successful place. The arm is not involved -- this is here
            # to validate goal logic, not to claim manipulation works.
            _, prop, px, py = step
            self.carrying = None
            mars.drop_prop_at(prop, px, py)
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "wait":
            mars.set_cmd_vel(0.0, 0.0)
            if self._t0 is None:
                self._t0 = t
            if t - self._t0 >= step[1]:
                self._t0 = None
                self._advance()
        else:
            raise ValueError(f"unknown plan op {op!r}")

    def _advance(self) -> None:
        self.i += 1

    def _drive_to(self, mars, tx: float, ty: float) -> bool:
        x, y, yaw = mars.pose()
        dx, dy = tx - x, ty - y
        dist = math.hypot(dx, dy)
        if dist <= ARRIVE_M:
            mars.set_cmd_vel(0.0, 0.0)
            return True
        # pose() returns qpos straight out, so yaw is already RADIANS. Passing
        # it through math.radians() again scaled 90 degrees down to 0.03 rad,
        # and the oracle drove confidently in the wrong direction on every map.
        err = _wrap(math.atan2(dy, dx) - yaw)
        # Turn in place until roughly aimed, then drive: a differential base
        # that creeps forward while badly misaligned spirals instead of arriving.
        if abs(err) > FACE_RAD:
            mars.set_cmd_vel(0.0, max(-W_MAX, min(W_MAX, 2.0 * err)))
        else:
            mars.set_cmd_vel(min(V_MAX, 0.6 * dist + 0.08), max(-W_MAX, min(W_MAX, 1.5 * err)))
        return False
