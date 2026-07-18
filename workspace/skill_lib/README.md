# skill_lib — shared primitives for skills

Plain Python modules skills import directly. No `Skill` classes here; no
registration, no roster entry, no invoker round trip — just functions that
take the interfaces they need as arguments.

```python
from workspace.skill_lib import arm as armlib

armlib.open_checked(self.manipulation, self._gripper_j6)
armlib.move_checked(self.manipulation, x, y, z, pitch=1.3, logger=self.logger)
armlib.rest(self.manipulation, self.joint_states)
armlib.go(self.manipulation, CARRY if holding else armlib.ZERO, times=2)
```

## What belongs where

- **Here**: short blocking device commands, recovery sequences (servo trip /
  brownout handling), reach clamps, pure math (camera geometry). Functions
  the *author of a skill* calls.
- **A `Skill`**: anything the agent or an operator invokes by name, and
  anything long-running enough to need cancellation checks and step
  telemetry. The moment a second skill wants to copy a private helper,
  that helper moves here.

## Two rules

1. **Import at the top of the skill file, never inside a function.** The
   skill loader puts the repo root on `sys.path` only while the skill module
   executes; a lazy import inside `execute()` will not resolve.
2. **Editing a lib file mid-session:** `/brain/reload_primitives` evicts
   `workspace.skill_lib*` from `sys.modules` before reloading skills, so a
   normal skills reload picks up lib edits too (see `catalog.reload_all`).
