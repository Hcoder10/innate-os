import sys
sys.path.insert(0, 'sim/bench')
from backends_v2 import _TaskStack, GOAL_CAP, CONSTRAINT_CAP

failures = []


def check(name, cond):
    print(f"{'OK  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


# 1. Partial goal re-list does NOT drop the goal that was omitted.
s = _TaskStack()
s.apply({"goals": [{"id": "gate1", "side": "R"}, {"id": "gate2", "side": "L"}]})
s.apply({"goals": [{"id": "gate2", "side": "L", "note": "cleared"}]})  # forgot to re-mention gate1
check("partial re-list preserves omitted goal", any(g["id"] == "gate1" for g in s.goals))
check("partial re-list still updates the mentioned goal",
      next(g for g in s.goals if g["id"] == "gate2").get("note") == "cleared")
check("goal count is 2, nothing duplicated", len(s.goals) == 2)

# 2. Explicit "done" DOES remove a goal, even with no "goals" key at all.
s.apply({"done": ["gate1"]})
check("explicit done removes exactly that goal (no 'goals' key needed)",
      [g["id"] for g in s.goals] == ["gate2"])

# 3. done accepted as a bare id, not only a list.
s.apply({"done": "gate2"})
check("bare-string done also removes the goal", s.goals == [])

# 4. Numeric ids are coerced, not silently dropped -- the regression an
#    earlier version of this fix introduced (strict isinstance(id, str)
#    meant a model emitting numeric ids lost every goal forever, which is
#    WORSE than the destructive-replace bug being fixed).
s2 = _TaskStack()
s2.apply({"goals": [{"id": 1, "side": "R"}]})
check("numeric id is coerced and kept, not dropped", [g["id"] for g in s2.goals] == [1])
s2.apply({"done": [1]})  # done side also needs to match a numeric id via str()
check("numeric id also matches on the done side", s2.goals == [])

# 5. Goals with no usable id are dropped AND counted, not silently vanished.
s3 = _TaskStack()
before = s3.dropped
s3.apply({"goals": [{"id": "ok"}, {"no_id": True}, "not a dict", 5, {"id": ["bad"]}]})
check("malformed goal entries are dropped without crashing", [g["id"] for g in s3.goals] == ["ok"])
check("dropped counter increments for each unusable entry", s3.dropped - before == 4)

# 6. Non-dict update to apply() itself is a safe no-op.
s3.apply("not a dict at all")
check("non-dict update to apply() is a safe no-op", [g["id"] for g in s3.goals] == ["ok"])

# 7. facts still merge (unchanged behavior), never dropped by an unrelated update.
s4 = _TaskStack()
s4.apply({"facts": {"delivered": "true"}})
s4.apply({"goals": [{"id": "x"}]})  # unrelated update, should not touch facts
check("facts survive an unrelated goals-only update", s4.facts.get("delivered") == "true")

# 8. constraints are additive, de-duplicated, capped -- AND re-mention
#    refreshes position instead of aging out under first-occurrence order.
#    This is the exact bug class being fixed, reintroduced in miniature by
#    a naive first pass at this same fix -- caught before shipping.
s5 = _TaskStack()
s5.apply({"constraints": ["important: do not claim X"]})
for i in range(CONSTRAINT_CAP):
    s5.apply({"constraints": [f"c{i}"]})
    s5.apply({"constraints": ["important: do not claim X"]})  # re-asserted every round
check("a constraint re-asserted every round survives past its cap-worth of neighbours",
      "important: do not claim X" in s5.constraints)
check("constraints still capped", len(s5.constraints) <= CONSTRAINT_CAP)
check("constraints de-duplicated", len(s5.constraints) == len(set(s5.constraints)))

# 9. A constraint that stops being re-mentioned DOES eventually age out
#    (append-only does not mean unbounded -- the cap still does its job).
s6 = _TaskStack()
s6.apply({"constraints": ["stale one"]})
for i in range(CONSTRAINT_CAP):
    s6.apply({"constraints": [f"c{i}"]})
check("an un-repeated constraint ages out past the cap", "stale one" not in s6.constraints)

# 10. note_released writes a fact mechanically, independent of apply(), and
#     records position and time when given them.
s7 = _TaskStack()
s7.note_released("test_item", (1.234, -0.5, 0.0), elapsed_s=42.4)
check("note_released writes a fact with position and time",
      s7.facts.get("released:test_item") == "true,at(1.23,-0.5),t=42s")
s8 = _TaskStack()
s8.note_released("test_item", None)
check("note_released degrades gracefully with no pose/time", s8.facts.get("released:test_item") == "true")

# 11. Goal count backstop: pathological growth (e.g. runaway id drift)
#     cannot grow the goal list without bound.
s9 = _TaskStack()
for i in range(GOAL_CAP + 25):
    s9.apply({"goals": [{"id": f"g{i}"}]})
check("goal list capped despite unbounded distinct ids", len(s9.goals) == GOAL_CAP)
check("the most recent goal survives the cap", s9.goals[-1]["id"] == f"g{GOAL_CAP + 24}")

# 12. The cap evicts by RECENCY OF UPDATE, not by original insertion order --
# this is the exact scenario an adversarial review demonstrated failing
# against a naive `by_id[gid] = g` (Python keeps an updated key's ORIGINAL
# dict position, so a plain overwrite does not move it to the end): one
# goal inserted first and then legitimately updated on every subsequent
# turn must survive, while a pile of newer-but-never-touched stale ids
# (simulating id-drift duplicates) are the ones that should get evicted.
s10 = _TaskStack()
s10.apply({"goals": [{"id": "alive", "note": "v0"}]})
for i in range(GOAL_CAP + 10):
    s10.apply({"goals": [{"id": f"stale{i}"}, {"id": "alive", "note": f"v{i + 1}"}]})
check("a goal updated every round survives the cap despite being the oldest insertion",
      any(g["id"] == "alive" for g in s10.goals))
check("its latest update value is preserved, not a stale earlier one",
      next((g for g in s10.goals if g["id"] == "alive"), {}).get("note") == f"v{GOAL_CAP + 10}")

# 13. bool and empty-string ids are rejected as malformed (not silently
# accepted and collapsed together -- bool is a subtype of int in Python,
# and an empty string looks valid but is not a real id).
s11 = _TaskStack()
before = s11.dropped
s11.apply({"goals": [{"id": True}, {"id": False}, {"id": ""}, {"id": "real"}]})
check("bool and empty-string ids are dropped as malformed, not accepted",
      [g["id"] for g in s11.goals] == ["real"])
check("dropped count reflects all three rejected entries", s11.dropped - before == 3)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASSED")
