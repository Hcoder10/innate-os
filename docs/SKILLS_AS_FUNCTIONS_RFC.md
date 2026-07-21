# RFC: Skills as functions

Status: draft — for team discussion. Nothing in this document is implemented;
the companion change (the curated cockpit Skills menu) stands on its own.

## Principle

Two axioms, from first principles:

1. **Existence is Python.** A skill is a function. Sharing code between skills
   is `import`, not a second kind of module with a different calling convention.
2. **Exposure is the consumer's list.** No skill decides where it appears.
   Every surface that shows skills owns an explicit list: an agent has
   `get_skills()`, the cockpit menu has `DEFAULT_COCKPIT_SKILLS` (plus the
   active directive's list), and skill code has imports. Author-side
   visibility metadata (hidden flags, folders, tags) is explicitly rejected:
   with N surfaces it becomes N annotations on every file, maintained forever,
   and the author is the one person who *doesn't* know what each surface needs.

Both of today's pains — poor reuse between skills, and a picker that lists
everything — come from fusing "is code" with "is a button" with "is an agent
tool". These axioms split them.

## Today

A skill is a `Skill` subclass: `name`, `guidelines()`, `execute(**params)`,
`Interface`/`RobotState` class attributes injected by the loader
(`brain_client/skills/{types,catalog,loader}.py`). Reuse paths:

- Skill → skill: already a plain call (`from innate.skills import wave`),
  routed through the run's `SkillInvoker`.
- Shared helpers: `workspace/skill_lib/` — plain functions, **not** invocable
  on their own. Code lands there only because making it a `Skill` costs class
  ceremony and (until the curated menu) forced UI exposure.

## Proposal

### A `@skill` decorator over a plain function

```python
# workspace/innate_skills/gripper_open.py
from innate import skill, SkillContext

@skill
def gripper_open(ctx: SkillContext, percent: float = 100.0, duration: float = 1.0):
    """Open the gripper/claw. percent=100 (default) is fully open."""
    ok = ctx.arm.open_checked(percent=percent, duration=duration)
    if not ok:
        ctx.fail("Gripper did not reach the target")
```

- The docstring is the guidelines; the signature is the input schema (the
  catalog already introspects `execute()` signatures — same code path).
- `ctx` carries what class attributes carry today: interfaces (`ctx.arm`,
  `ctx.mobility`, `ctx.head`, `ctx.camera`), robot state, `ctx.logger`,
  `ctx.feedback(...)`, `ctx.check_cancelled()`, `ctx.fail(...)`. Interfaces
  are connected lazily on first access, so a function only pays for what it
  touches (replaces the `Interface(...)` declarations).
- Return value: `None` = success; a string = success message; raise
  `SkillFailed`/`SkillCancelled` as today.
- **Plumbing, not a rewrite:** `@skill` manufactures the same `Skill` subclass
  the loader already handles (`execute` delegating to the function, cancel
  latch, telemetry, hot reload). Class-based skills keep working indefinitely;
  files migrate opportunistically when touched.

### `skill_lib` narrows to its honest core

The rule becomes mechanical — no more judgment calls in review:

- **Touches the robot → it's a skill** (a `@skill` function), even if no
  operator ever taps it. It stays importable, CLI-runnable, and listable by
  any directive; the cockpit shows it only if a list names it.
- **Pure computation → `skill_lib`** (geometry, camera math, reach clamps).

Existing robot-action helpers in `skill_lib/arm.py` etc. migrate out
opportunistically; `geometry.py` and friends stay forever.

## What we deliberately don't build

Folders, tags, packs, hidden flags, or any author-side visibility metadata.
If flat curated lists ever stop scaling, the escape hatch is search in the
picker — not taxonomy.

## Sequencing

1. (Done, separate change) Curated cockpit menu — consumer-side list, webapp
   only.
2. `@skill` decorator + `SkillContext` in `brain_client/skills/`, new skills
   use it.
3. Migrate shipped skills file-by-file as they're touched; move robot actions
   out of `skill_lib` the same way.

## Open questions

- `SkillContext` surface: exactly which interface/state accessors, and does
  `ctx.skills.<name>()` replace the `innate.skills` proxy imports or live
  beside them?
- Chaining semantics for function skills invoked from other skills (today's
  invoker nesting rules carry over, but spell it out).
- Does `execute()`-signature introspection need extension for `ctx` (skip the
  first param — mirror of skipping `self` today)?
