import sys, math
sys.path.insert(0, 'sim/bench')
from brain_agent import BrainAgent, PRIMITIVE_TIMEOUT_S, Observation


class FakeMars:
    def __init__(self, pose=(0.0, 0.0, 0.0)):
        self._pose = pose

    def pose(self):
        return self._pose

    def set_cmd_vel(self, v, w):
        pass


class FakeBackend:
    wants_image = False
    wants_pose = False


class FakeChallenge:
    brief = "test"


failures = []


def check(name, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def blocked_forward(agent, mars, start_pose, moved_frac_target, target_m=1.5):
    """Simulate a forward primitive that times out having covered only a
    small fraction of its target -- matches the traced 'gave up ... probably
    blocked' pattern."""
    agent._prim = ("forward", target_m, 0.0, start_pose)
    x, y, yaw = start_pose
    mars._pose = (x + moved_frac_target * target_m, y, yaw)
    return agent._step_primitive(mars, PRIMITIVE_TIMEOUT_S + 0.1)


def completed_turn(agent, mars, start_pose, degrees):
    """Simulate a turn primitive that actually reaches its target heading."""
    agent._prim = ("turn", math.radians(degrees), 0.0, start_pose)
    x, y, yaw = start_pose
    mars._pose = (x, y, yaw + math.radians(degrees))
    return agent._step_primitive(mars, 1.0)


def completed_forward(agent, mars, start_pose, metres):
    agent._prim = ("forward", metres, 0.0, start_pose)
    x, y, yaw = start_pose
    mars._pose = (x + metres, y, yaw)
    return agent._step_primitive(mars, 1.0)


agent = BrainAgent(FakeBackend())
mars = FakeMars()

# 1. Basic increment on a blocked forward.
blocked_forward(agent, mars, (0.0, 0.0, 0.0), 0.05)
check("blocked_streak increments on a timed-out forward", agent._blocked_streak == 1)

# 2. THE MOTIVATING SCENARIO, replayed directly: blocked forward, a
# SUCCESSFUL recovery turn, blocked forward again -- this is the exact
# pattern a traced episode showed (see FINDINGS.md T18) and is what an
# earlier version of this fix was inert against, because resetting on ANY
# completed primitive (including the turn) wiped the count every cycle
# and it could never climb past 1. This is the one test that would have
# caught that before it shipped, not after.
agent._blocked_streak = 0
blocked_forward(agent, mars, (0.0, 0.0, math.radians(74)), 0.26)
check("cycle 1: blocked forward -> streak 1", agent._blocked_streak == 1)
completed_turn(agent, mars, mars._pose, 75)
check("a completed TURN does NOT reset the streak", agent._blocked_streak == 1)
blocked_forward(agent, mars, mars._pose, 0.26)
check("cycle 2: streak climbs to 2 despite the intervening successful turn",
      agent._blocked_streak == 2)
completed_turn(agent, mars, mars._pose, 75)
blocked_forward(agent, mars, mars._pose, 0.26)
check("cycle 3: streak climbs to 3, matching the traced episode", agent._blocked_streak == 3)

# 3. Only a completed FORWARD resets it -- proves the path was actually clear.
completed_forward(agent, mars, mars._pose, 1.5)
check("a completed forward resets the streak to 0", agent._blocked_streak == 0)

# 4. A blocked TURN also counts toward the streak (both primitive kinds can
# discover the robot cannot move the way it just tried to).
agent._prim = ("turn", math.radians(90), 0.0, (0.0, 0.0, 0.0))
mars._pose = (0.0, 0.0, math.radians(2))  # barely turned
agent._step_primitive(mars, PRIMITIVE_TIMEOUT_S + 0.1)
check("a timed-out turn also increments the streak", agent._blocked_streak == 1)

# 5. Non-movement actions go through the REAL _apply() dispatch (not a
# hand-simulated result) for the actions that need no mars interaction --
# look/say/answer/finish/unknown -- and must leave the streak untouched.
# Static analysis (grep) shows _apply() never references _blocked_streak
# anywhere, but this exercises that property through the actual code path
# rather than trusting the grep alone.
for action in ("look", "say", "answer", "finish", "totally_unknown_action"):
    agent._blocked_streak = 2
    agent._apply(None, 0.0, {"action": action, "args": {}})
    check(f"_apply()'s real '{action}' branch leaves streak untouched",
          agent._blocked_streak == 2)

# 6. reset() clears the streak across episodes.
agent._blocked_streak = 3
agent.reset(mars, FakeChallenge())
check("reset() clears blocked_streak", agent._blocked_streak == 0)

# 7. End-to-end wiring: _observe() itself (not a hand-built Observation)
# carries the real _blocked_streak into the Observation it returns, and
# the warning threshold/content is driven by that real value.
agent._blocked_streak = 3
obs = agent._observe(mars, 10.0)
check("_observe() carries the real blocked_streak through", obs.blocked_streak == 3)
check("the resulting Observation's as_text() includes the warning",
      "NOTE:" in obs.as_text() and "3 times" in obs.as_text())

agent._blocked_streak = 1
obs_low = agent._observe(mars, 10.0)
check("_observe() at streak 1 produces no warning", "NOTE:" not in obs_low.as_text())

# 8. Warning text does not assume "Last action" is the blocked one (it may
# not be, if a non-movement action happened since) and does not prescribe
# actions unavailable to a blind backend (no "look" recommendation baked
# into shared text).
warn_text = Observation(brief="x", elapsed_s=0.0, blocked_streak=3).as_text()
check("warning does not hardcode an ordinal like '4th time'", "4th" not in warn_text)
check("warning does not assume Last action is the blocked one",
      "see 'Last action'" not in warn_text)
check("warning does not prescribe 'look' (unavailable to blind backends)",
      "look first" not in warn_text.lower())

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASSED")
