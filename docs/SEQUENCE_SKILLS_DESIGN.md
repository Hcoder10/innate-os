# RFC: Skill Chaining — invoker plumbing, plain-function authoring

**Status:** Draft / proposal · **Author:** Dev Rel · **Date:** 2026-06-30 · **Updated:** 2026-07-01

> **Terminology:** "digital skills" are being renamed to **code skills** (the Python `Skill` subclasses, as opposed to the physical `learned`/`replay` skills). This doc uses "code skills" throughout.

## Problem

Users want to chain skills in a fixed order — e.g. *learned grasp policy → classic drive-forward → another policy → mimic playback* — and have it run **predictably and visibly** every time. Today the only way is a numbered list in a directive's `get_prompt()` that the cloud LLM interprets reactively, one `next_primitive` at a time. It's **invisible** (nothing in the authoring API hints chaining exists) and **non-deterministic** (the model can skip or reorder steps). Three shipped directives already hand-roll this prose pattern — [`chess_agent`](../workspace/innate_agents/chess_agent.py), [`security_guard_agent`](../workspace/innate_agents/security_guard_agent.py), [`board_calibration_agent`](../workspace/innate_agents/board_calibration_agent.py) — which is the signal it wants a real construct.

## Proposal: two layers

1. **Plumbing (implemented):** a `SkillInvoker` that lets a code skill run any other skill — code, `learned`, or `replay` — through the existing dispatch paths, with per-step lifecycle events the app already renders. Exposed as `self.skills.run(id, **inputs)`.
2. **Authoring surface (this update):** skills as **importable functions**. Chaining is just calling them. The invoker becomes the low-level form; users normally never see it.

```python
from innate.skills import head_emotion, navigate_to_position, open_drawer_policy, pour_mimic

class MorningRoutine(Skill):
    def execute(self):
        head_emotion(emotion="excited")
        navigate_to_position(x=0.3, y=0.0, theta=0.0, local_frame=True)
        open_drawer_policy()          # learned policy — same call shape
        pour_mimic()                  # replay — same call shape
        return "Routine complete", SkillResult.SUCCESS
```

No new execution `type`, no new authoring concept, no list-of-IDs DSL: a chain is a code skill whose body calls other skills like functions. "Stop on first failure" is Python's own semantics (see failure model below), so there's no status-checking loop to write.

## The authoring surface

### How it works

- **`innate.skills` is a proxy module** (module-level `__getattr__`, PEP 562). Looking up an attribute resolves the name against the skill catalog and returns a thin callable. Nothing is imported eagerly; learned/replay skills that only exist at runtime resolve the same way.
- **An ambient invoker via `contextvars.ContextVar`.** When the skills server starts a skill, it sets the active `SkillInvoker` in a context variable (set/reset around `execute()`, in the thread that runs it; the invoker does the same around each child, so nested chains follow). A proxy call is just `_current_invoker.get().run(resolved_id, **kwargs)`. Called outside a running skill, it raises immediately with *"skills can only be called from within execute()"*. This implicit-context pattern is the same one Temporal, Prefect, and pytest fixtures use.
- **IDE completion from a generated stub.** The loader knows the catalog, so it generates `innate/skills.pyi` with one signature per skill (docstrings from `guidelines()`). Regenerated whenever the catalog changes (hot reload, newly trained policy) so completion never lies. The stub doubles as a browsable skill reference.

### Failure model: raise, don't return

Proxy calls do **not** return `(message, SkillResult)`:

- **Success** returns the child's output message — so passing one step's result to the next is `pose = detect_board()`, no tuple unpacking.
- **Failure** raises `SkillFailed(message)`.
- **Cancellation** raises `SkillCancelled` — an exception is what *guarantees* the routine unwinds instead of marching to the next step (stronger than the invoker's `_cancelled` flag).

An unhandled `SkillFailed`/`SkillCancelled` escaping `execute()` is converted by the server into the parent's failure/cancel result with the child's message. Recovery is ordinary `try/except SkillFailed`. The low-level `self.skills.run()` keeps its tuple return unchanged — two call surfaces, two conventions, never retrofit raising onto `run()`.

### Backward compatibility

Everything here is additive; **no existing skill changes behavior or needs migration**:

- The `Skill` base class, the `(message, SkillResult)` return convention, `cancel()`, `Interface`/`RobotState` descriptors, and the loader contract are untouched.
- `self.skills.run(id, **inputs)` remains as the documented low-level form — needed anyway for dynamic IDs ("run whichever policy the detector picked").
- The contextvar is set for *every* skill run, so an existing class skill can adopt proxy calls one line at a time; mixing `head_emotion(...)` and `self.skills.run(...)` in one body works — both bottom out in the same invoker.
- Testing: a routine under unit test sets the contextvar to a fake invoker — a small pytest fixture shipped with the SDK.

### Decisions to settle before shipping

| Decision | Recommendation |
|----------|----------------|
| Public namespace home | Introduce `innate.skills` (and re-export `innate.Skill`) rather than exposing `brain_client.*` paths — this import line appears in every skill users ever write, so its name is API surface. Old `brain_client.skills.types` imports keep working. |
| Name collisions | Flat namespace; `local/` shadows `innate-os/` with a load-time warning. `self.skills.run("innate-os/…")` always disambiguates. |
| Threading | The contextvar must be set in the thread that actually runs `execute()`. Trivial if that's the goal-callback thread, but it's the one place a subtle bug could live — needs a test, not an assumption. |
| Stub regeneration hook | On catalog change (loader + hot-reload watcher). |

### The tradeoff, stated honestly

The cost of Level 2 is **implicitness**: `head_emotion(...)` works or doesn't depending on ambient execution context, and the call site doesn't show the machinery. Mitigations: fail loudly outside a skill run, keep the explicit `run()` form documented, ship the test fixture. We judge the readability of every user's routine worth one well-precedented piece of magic.

## The steps show up in the UI for free

The app renders steps from **lifecycle events, not metadata**: it groups `task_activated` messages keyed by `primitiveName`/`primitiveId` + `taskStatus` (running/completed/failed) into step cards ([ros.ts:303](../innate-controller-app/src/types/ros.ts), [ChatContext.tsx:239](../innate-controller-app/src/context/ChatContext.tsx)). The invoker emits the same `primitive_lifecycle_message` per child that the runner emits today for top-level skills, so every sub-step appears as its own running→completed card — **with zero app changes**. (Optional polish: add a `parentPrimitiveId` field to visually group sub-steps under the Routine.) Proxy calls inherit this for free since they bottom out in the invoker.

## Implemented (Phase 1 — plumbing)

- [`skills/invoker.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/invoker.py) — `SkillInvoker`, injected onto each code skill as `self.skills`. `run(id, **inputs)` runs a code or physical child to completion and returns `(message, SkillResult)`; `cancel()` stops the active child.
- [`skills_server.py`](../ros2_ws/src/brain/brain_client/brain_client/nodes/skills_server.py) — extracted `_run_code_skill_body` / `_run_physical_skill` (faithful, no behavior change) so a child reuses the exact top-level execution paths without finalizing the parent's goal; injects the invoker before `execute()`.
- [`runner.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/runner.py) + [`lifecycle.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/lifecycle.py) — children piggyback a tagged lifecycle marker on the parent's feedback; the runner re-emits it as a per-step `task_activated` status **to the app only** (not the cloud agent, which runs one primitive at a time and must keep seeing just the parent).
- Example: [`run_routine_demo.py`](../workspace/innate_skills/run_routine_demo.py) + [`routine_demo_agent.py`](../workspace/innate_agents/routine_demo_agent.py). Unit tests in `test/test_skill_invoker.py`.

Known v1 limits: an orchestrator should not itself declare required robot states (single-slot continuous-state tracking); deep nested-chain cancel is best-effort; a chaining skill's `cancel()` must call `self.skills.cancel()` (Phase 2 removes this: the base class's cancel delegates to the active invoker by default, making the demo's `cancel()` deletable).

## Implemented (Phase 2 — authoring surface)

- [`innate/skills.py`](../ros2_ws/src/brain/brain_client/innate/skills.py) — the proxy module: module `__getattr__` returns per-name proxies, `use_invoker()` context manager owns the ambient-invoker contextvar (and doubles as the test fixture), `SkillFailed`/`SkillCancelled` exceptions. [`innate/__init__.py`](../ros2_ws/src/brain/brain_client/innate/__init__.py) re-exports the authoring surface (`Skill`, `SkillResult`, descriptors, exceptions). Installed as a second package from the same CMake project.
- [`skills_server.py`](../ros2_ws/src/brain/brain_client/brain_client/nodes/skills_server.py) — `_run_code_skill_body` wraps `execute()` in `use_invoker(skill.skills)` (same thread, so nesting and the contextvar are correct by construction) and translates escaping `SkillFailed`/`SkillCancelled` into the parent's `(message, status)`.
- [`invoker.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/invoker.py) — `run()` now also accepts bare names; resolution tries `local/` before `innate-os/`, so a user's skill shadows a shipped one.
- [`types.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/types.py) — `Skill.cancel()` is no longer abstract: the default delegates to `self.skills.cancel()`, so a pure orchestrator needs no `cancel()` at all.
- [`run_routine_demo.py`](../workspace/innate_skills/run_routine_demo.py) rewritten in the new style. Tests in `test/test_skill_proxy.py` + bare-name cases in `test/test_skill_invoker.py`.
- **Skills can speak: `self.say(text)`** ([`types.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/types.py)) — fire-and-forget TTS through the robot's voice. Publishes to `/brain/tts`, which `brain_client_node` already routes to Cartesia + the speaker; returns immediately (speech overlaps motion), no-ops safely without a node (tests) or without audio. Verified end-to-end on the robot (~1.3s to first audio).
- **User skills override shipped ones by name, everywhere.** Bare-name chaining already resolved `local/` before `innate-os/`; [`catalog.py`](../ros2_ws/src/brain/brain_client/brain_client/skills/catalog.py)'s display-name dedup now applies the same precedence to the published skills list, so the cloud agent and `innate.skills` imports agree on which skill a name means (`_dedupe_display_names`, logged as a shadow warning; the shipped skill stays runnable via its full id). Tests in `test/test_skill_name_override.py`.

Still open from Phase 2: `.pyi` stub generation for IDE completion (needs a decision on where the stub lives so user editors pick it up) and converting a real shipped directive off its prose loop.

Known sharp edges (assessed post-implementation):

- **Cancel re-entrancy (fixed).** A mid-run child using the default `cancel()` delegates back to the invoker cancelling it; without a guard this recursed ~1000 frames per cancel, silently (the `RecursionError` was swallowed by the invoker's catch-all). `SkillInvoker.cancel()` now carries a re-entrancy guard; regression test asserts the child's cancel runs exactly once.
- **Default `cancel()` removes the forcing function.** `cancel()` used to be abstract, so every author at least thought about it. Now a leaf skill doing long blocking work can silently omit it and become uncancellable. Docs must say: the default only stops *chained children*; if execute() does its own long work, override cancel().
- **No recursion/cycle guard on chains.** A skill that runs itself (or A→B→A) recurses until Python's limit, then surfaces as a step failure. Degrades safely but confusingly; a depth cap in the invoker is the fix if it ever bites.
- **Import names are file stems, not display names.** `from innate.skills import my_routine` matches `my_routine.py` / the skill directory name — not the display name the app shows. Stub generation will paper over this; until then it's a docs footnote.
- **Skill instances are singletons.** A child invoked mid-chain is the same object as its top-level runs; skills that carry state across execute() calls (e.g. a `_cancelled` flag not reset at the top of execute) misbehave more often under chaining. Pre-existing, but chaining raises the exposure.
- **Proxies are main-thread only.** contextvars don't flow into user-spawned threads, so a proxy called from a background thread raises the "only while a skill is executing" error (deliberate — parallel chaining is explicitly out of scope for now).
- **Long routines look like one long primitive to the cloud.** Children report steps to the app only; worth checking the cloud side has no per-primitive timeout that a 10-minute routine would trip.

## Phasing

1. **Invoker + lifecycle-per-step** — done (above).
2. **Authoring surface:** `innate.skills` proxy module, contextvar wiring, `SkillFailed`/`SkillCancelled`, default `cancel()` delegation, stub generation, test fixture. Convert one real directive (e.g. patrol) off its prose loop and rewrite `run_routine_demo` in the new style as the docs example.
3. **`parentPrimitiveId`** for grouped step display in the app.
4. **Pre-run preview / declared steps**, if no-code becomes a goal. Note: imports aren't load-time-validatable declarations, so preview would come from statically scanning `execute()` bodies or an opt-in declaration for tooling-critical routines — deferred.

## Alternatives considered

- **Bound-attribute calls only (`self.skills.head_emotion(...)`), no imports.** Same call shape, no ambient context. Rejected as the headline because the import line *is* the discoverability story — examples show `from innate.skills import …` and the model is instantly clear, with stub-backed completion. (This form may still fall out of the implementation for free.)
- **`@skill` decorator (routine-as-function, no class).** Demos beautifully — a whole routine in six lines — but introduces a second authoring style whose boundary ("when do I need a class?") must be documented forever. Deferred; nothing in Phase 2 forecloses it.
- **`UseSkill` class-level descriptors** (declare dependencies like `Interface`/`RobotState`, call them in `execute()`). Gives load-time validation and preview metadata, but adds per-skill declaration boilerplate that plain imports don't have. Revisit as the opt-in declaration form if Phase 4 happens.
- **New `type: "sequence"` skill (declarative).** Cleaner for pre-run preview/validation, but it's literally a list-of-steps DSL: less general than Python composition, falls off a cliff at the first conditional or retry, and adds a new concept users must learn.
- **Agent-API construct (`get_routines()`).** Couples chains to a directive, so they can't be reused or surfaced like other skills.
- **Docs/template only.** Closes the discoverability gap but leaves ordering on the LLM — not deterministic.
