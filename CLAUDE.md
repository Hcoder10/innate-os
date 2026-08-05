# CLAUDE.md

Project instructions for Claude. See [AGENTS.md](AGENTS.md) for the system overview, the
`innate` CLI, and the ROS package map.

## Writing skills

### Never `time.sleep` — always `self.sleep`

**In skill code, use `self.sleep(seconds)`. Never `time.sleep(seconds)`.**

`self.sleep` wakes and raises `SkillCancelled` the moment a Stop lands; `time.sleep` blocks
to completion, so a skill that uses it keeps running (and keeps the robot moving) after the
user pressed Stop. Sleeping is the only cancel point a loop needs — write the loop as if
cancel didn't exist and let the framework halt the base and report `CANCELLED`.

```python
while traveled < target:
    self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
    self.sleep(0.1)          # ✅ cancellable
    # time.sleep(0.1)        # ❌ Stop is ignored until the sleep finishes
```

`time` itself is fine for *measuring* — `time.time()` / `time.monotonic()` for deadlines and
elapsed checks. The rule is only about blocking.

`self.wait_for(read, timeout)` and `self.check_cancelled()` are cancel-aware too; cleanup
belongs in `try/finally`.

**The one exception:** teardown and already-committed physical actions must *not* be
cancellable, so they use `time.sleep` deliberately — e.g. once `pick_any_object` closes the
gripper, a cancel must not unwind mid-grip and drop the object. If you write such a section,
comment it, or the next reader will "fix" it back to `self.sleep` and reintroduce the bug.

See [AGENTS.md](AGENTS.md#writing-skills) for the full cancellation contract.
@AGENTS.MD