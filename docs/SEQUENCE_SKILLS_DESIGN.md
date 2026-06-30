# RFC: Skill Chaining via a Skill Invoker

**Status:** Draft / proposal · **Author:** Dev Rel · **Date:** 2026-06-30

> **Terminology:** "digital skills" are being renamed to **code skills** (the Python `Skill` subclasses, as opposed to the physical `learned`/`replay` skills). This doc uses "code skills" throughout.

## Problem

Users want to chain skills in a fixed order — e.g. *learned grasp policy → classic drive-forward → another policy → mimic playback* — and have it run **predictably and visibly** every time. Today the only way is a numbered list in a directive's `get_prompt()` that the cloud LLM interprets reactively, one `next_primitive` at a time. It's **invisible** (nothing in the authoring API hints chaining exists) and **non-deterministic** (the model can skip or reorder steps). Three shipped directives already hand-roll this prose pattern — [`chess_agent`](../workspace/innate_agents/chess_agent.py), [`security_guard_agent`](../workspace/innate_agents/security_guard_agent.py), [`board_calibration_agent`](../workspace/innate_agents/board_calibration_agent.py) — which is the signal it wants a real construct.

## Proposal: a skill invoker, not a new skill type

Let a code skill invoke other skills through an injected handle:

```python
def execute(self):
    self.skills.run("innate-os/navigate_with_vision", target="doorway")
    self.skills.run("local/open_drawer_policy")          # learned
    self.skills.run("innate-os/navigate_to_position", x=1.0, y=0.0, theta=0.0)
    self.skills.run("local/pour_mimic")                  # replay
```

The invoker reuses the existing dispatch path (skills server for code, behavior server for `learned`/`replay`) and gates on each child's `SkillResult` before advancing. This gives full composition — loops, conditionals, retries, passing one step's output to the next — as plain Python, across all skill kinds, because they already share one interface.

No new execution `type` is needed: a chain is just a **code skill** that orchestrates others, presented in the UI as a "Routine."

## The steps show up in the UI for free

The app renders steps from **lifecycle events, not metadata**: it groups `task_activated` messages keyed by `primitiveName`/`primitiveId` + `taskStatus` (running/completed/failed) into step cards ([ros.ts:303](../innate-controller-app/src/types/ros.ts), [ChatContext.tsx:239](../innate-controller-app/src/context/ChatContext.tsx)). So if the invoker emits the same `primitive_lifecycle_message` per child that the runner emits today for top-level skills, every sub-step appears as its own running→completed card — **with zero app changes**. (Optional polish: add a `parentPrimitiveId` field to visually group sub-steps under the Routine.)

## What's needed

| Area | File | Change |
|------|------|--------|
| Invoker | [`skills/runner.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/runner.py) | A `self.skills.run(id, **inputs)` handle: dispatch child via the existing path, await `SkillResult`, emit lifecycle event per child, propagate cancel. |
| Skill base | [`skills/types.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/types.py) | Inject the invoker handle into `Skill` (alongside interfaces/state). |
| One-in-flight invariant | runner | Allow a parent skill to hold a child task without the runner treating itself as idle. |
| Cloud agent / app | — | No change required for runtime step display. |

## Today vs. this proposal

Right now a skill can only emit text via `_send_feedback` (surfaces as `skill_output`, relayed through `handle_primitive_feedback` in the cloud) — so you can fake textual steps, but you **cannot actually invoke** other skills, and `learned`/`replay` live on a separate server unreachable from Python. The invoker is the missing piece; it's small and reuses the event path the app already renders.

## Optional: declared steps for pre-run preview

The invoker gives visibility *during* execution. To also preview/validate a chain *before* it runs (a step list in the UI, load-time check that step IDs resolve, no-code authoring), let a Routine **declare** its steps in metadata while still **executing** them via the invoker — declared steps + imperative body. Defer unless preview/no-code is a near-term goal.

## Phasing

1. **Invoker + lifecycle-per-step.** Convert one real directive (e.g. patrol) off its prose loop to prove it end-to-end.
2. **`parentPrimitiveId`** for grouped step display in the app.
3. **Declared steps** for preview/validation/no-code, if needed.

## Implemented (Phase 1)

- [`skills/invoker.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/invoker.py) — `SkillInvoker`, injected onto each code skill as `self.skills`. `run(id, **inputs)` runs a code or physical child to completion and returns `(message, SkillResult)`; `cancel()` stops the active child.
- [`skills_server.py`](../ros2_ws/src/brain/brain_client/brain_client/nodes/skills_server.py) — extracted `_run_code_skill_body` / `_run_physical_skill` (faithful, no behavior change) so a child reuses the exact top-level execution paths without finalizing the parent's goal; injects the invoker before `execute()`.
- [`runner.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/runner.py) + [`lifecycle.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/lifecycle.py) — children piggyback a tagged lifecycle marker on the parent's feedback; the runner re-emits it as a per-step `task_activated` status **to the app only** (not the cloud agent, which runs one primitive at a time and must keep seeing just the parent).
- Example: [`run_routine_demo.py`](../workspace/innate_skills/run_routine_demo.py) + [`routine_demo_agent.py`](../workspace/innate_agents/routine_demo_agent.py). Unit tests in `test/test_skill_invoker.py`.

Known v1 limits: an orchestrator should not itself declare required robot states (single-slot continuous-state tracking); deep nested-chain cancel is best-effort; a chaining skill's `cancel()` must call `self.skills.cancel()`.

## Alternatives considered

- **New `type: "sequence"` skill (declarative).** Cleaner for pre-run preview/validation, but less general than Python composition and adds a new concept users must learn. Folded in here as the optional "declared steps" layer instead.
- **Agent-API construct (`get_routines()`).** Couples chains to a directive, so they can't be reused or surfaced like other skills.
- **Docs/template only.** Closes the discoverability gap but leaves ordering on the LLM — not deterministic.
