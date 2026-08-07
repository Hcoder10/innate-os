# CLAUDE.md

Project instructions for Claude. See [AGENTS.md](AGENTS.md) for the system overview, the
`innate` CLI, and the ROS package map.

## Comments: minimal, why-only

Write code clear enough to need no comment; a comment is a last resort, not a habit.

A comment earns its place only when it states something the code *cannot* say — and then it
is 1-3 lines, no more:

- a physical/hardware fact (`# preload beyond this overcurrent-trips the servo`)
- a crash or race being designed around (`# destroying a sub under the spinning executor → InvalidHandle`)
- a deliberate exception to a project rule (`# committed: teardown must not be cancellable`)
- a misleading external interface (`# /ik_delta is an ABSOLUTE pose despite the name`)

Never write comments that narrate the next line, restate the function name, label obvious
sections of a short function, justify the change to a reviewer, or repeat something already
said once elsewhere (say the invariant in one place and reference it). Docstrings follow the
same rule: one tight paragraph of contract — no Args/Returns lists that restate
self-explanatory parameter names, no prose repeating the signature. If a comment is needed to
make code understandable, first try renaming or restructuring so it isn't.

## Code style

Code reads like prose; the best part is no part. In brief — the full rules and a worked
before/after live in [AGENTS.md](AGENTS.md#code-style):

- **Shape:** early returns and merged conditions over nesting; do-everything functions split
  into named steps; one concern per module, pure functions in a `utils.py` beside the caller.
- **Types:** every signature typed; collaborators under `TYPE_CHECKING`. Frozen dataclasses
  for internal objects, `TypedDict` only for wire-format dicts, StrEnums
  (`brain_client/common/enums.py`) for closed string vocabularies — wire values never change.
  `basedpyright` stays at 0 errors, fixed at the root, never `# type: ignore`.
- **try/except:** an invariant at a boundary (loop crash reporter, ROS callbacks, user code),
  not insurance. Narrow types for parses; per-item catches in loops; broad catches carry
  their justification.
- **Done means verified:** ruff check + format, the unit suite, and basedpyright — all clean
  before reporting completion.

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
@AGENTS.md
