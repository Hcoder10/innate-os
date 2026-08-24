import sys, os
sys.path.insert(0, 'sim/bench')
os.environ.setdefault("GEMINI_API_KEY", "fake-for-unit-test")
from backends_v2 import NemotronStackBackend


class FakeObs:
    def __init__(self, carrying, pose=(0.0, 0.0, 0.0), elapsed_s=0.0):
        self.carrying = carrying
        self.image_path = None
        self.robot_pose = pose
        self.elapsed_s = elapsed_s

    def as_text(self):
        return f"Carrying: {self.carrying or 'nothing'}"


b = NemotronStackBackend()
b._call = lambda *a, **kw: {"action": "look", "args": {}}  # no real network call

failures = []


def check(name, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


# Turn 1: nothing carried yet -- no checkpoint should fire.
b.decide(FakeObs(None), "menu")
check("no spurious fact on episode start", not b.stack.facts)

# Turn 2: agent is now carrying an item (pick succeeded before this obs).
b.decide(FakeObs("test_item_a", pose=(1.0, 2.0, 0.0), elapsed_s=10.0), "menu")
check("still no fact while carrying (not released yet)", not b.stack.facts)

# Turn 3: carrying cleared -- a place/release just happened at the PREVIOUS
# turn's pose and elapsed time (the robot does not move during a place
# primitive, and no sim time passes between the place and this observation
# in the harness's own accounting).
b.decide(FakeObs(None, pose=(1.0, 2.0, 0.0), elapsed_s=12.0), "menu")
check("fact written the turn carrying clears, with position and time",
      b.stack.facts.get("released:test_item_a") == "true,at(1.0,2.0),t=12s")

# Turn 4: still nothing carried -- must not re-fire or duplicate weirdly.
b.decide(FakeObs(None), "menu")
check("no re-trigger on a second empty-carrying turn", len(b.stack.facts) == 1)

# Now pick/place a second, different item -- must not clobber the first fact.
b.decide(FakeObs("test_item_b", pose=(3.0, -1.0, 1.57), elapsed_s=30.0), "menu")
b.decide(FakeObs(None, pose=(3.0, -1.0, 1.57), elapsed_s=31.0), "menu")
check("first item's fact survives a second pick/place cycle",
      b.stack.facts.get("released:test_item_a") == "true,at(1.0,2.0),t=12s")
check("second item also checkpointed at its own position/time",
      b.stack.facts.get("released:test_item_b") == "true,at(3.0,-1.0),t=31s")

# Re-pick the FIRST item (a legitimate "handle it again" re-task): its
# stale released: fact must be retired the moment the gripper closes on it
# again, not left sitting around contradicting obs.carrying.
b.decide(FakeObs("test_item_a", pose=(5.0, 5.0, 0.0), elapsed_s=50.0), "menu")
check("stale released: fact retired on re-pick",
      "released:test_item_a" not in b.stack.facts)
check("the OTHER item's fact is untouched by a re-pick",
      b.stack.facts.get("released:test_item_b") == "true,at(3.0,-1.0),t=31s")

# Re-release it -- must get a FRESH fact at the new pose/time, not the old one.
b.decide(FakeObs(None, pose=(5.0, 5.0, 0.0), elapsed_s=52.0), "menu")
check("re-released item gets a fresh fact at its new position/time",
      b.stack.facts.get("released:test_item_a") == "true,at(5.0,5.0),t=52s")

# reset() must clear the carrying-tracking state across episodes, or a
# fresh episode could spuriously fire a checkpoint from the PREVIOUS
# episode's last carried item.
b.reset()
check("reset() clears carrying-transition tracking", b._last_carrying is None)
check("reset() clears the stack itself", b.stack.facts == {})

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASSED")
