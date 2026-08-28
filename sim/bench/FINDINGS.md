# Findings

Every defect this benchmark has surfaced, and whose fault it was.

**Evidence.** The `results/` paths cited below -- the probe transcripts under
`results/claude_probe/` and `results/sonnet_probe*/`, the `_repeat_*` /
`_trace_*` / `_t19_recheck` records -- resolve on the fork branch
[`benchmark-evidence`](https://github.com/Hcoder10/innate-os/tree/benchmark-evidence/sim/bench/results).
They were moved out of the PR to keep it mergeable; the citations are left as
written and are valid on that branch.

The distinction matters more than the scores. A suite that cannot tell "the
robot failed" from "my harness failed" reports the second as the first, and
every number it produces is unfalsifiable. So each entry below is filed under
one of three headings, and the rule for filing is:

| | |
|---|---|
| **Harness** | The measuring apparatus was wrong. No agent could have scored correctly, and any number taken before the fix is void. |
| **Task** | The challenge or the world was wrong — unsolvable, unreachable, or gameable. The validity gate is meant to catch these before a number exists. |
| **Robot** | The system under test did the wrong thing. Only these are results. |

Across eight maps and 45 challenges: **twenty harness faults, fourteen task
faults** (several holding multiple instances; T5's number is retired -- its
mystery dissolved into H9 once solved -- and the T-series past T15 is a
chronological ledger whose entries each carry their own HARNESS/AGENT
verdict), and the robot results the suite exists to produce. The robot column stayed empty for a long time, and that
was the honest headline until the agent could actually see and actually be
heard. Two of the last three harness faults were the reason it was empty.

---

## Harness faults

### H1. Sim time ran ten times faster than the wall clock while the agent thought

`run_episode` steps the sim as fast as the CPU allows. Headless at 160×120 that
is roughly 10× real time. Every challenge time limit is denominated in *sim*
seconds, so a 4.8 s model call charged the robot ~48 s of world time.

The first LLM episode spent its entire 240 s budget on five turns and never
moved. Read naively, that is an agent that dithers. It was the apparatus
charging it an order of magnitude too much for thinking.

**Fix:** while an agent reports `thinking`, sim time is held to the wall clock —
1:1, the rate a real robot actually pays. Re-run: sim 22.1 s against wall 22 s.

**Voided:** every LLM number taken before `patch_thinktime.py`.

### H2. A well-formed model reply was reported as an agent failure

The action schema carries `args` as an escaped JSON *string*, because
closed-schema APIs reject free-form objects. So a valid reply legitimately
contains braces inside string literals:

```
{"action":"say","args":"{\"text\":\"I cannot see anything\"}"}
```

The extractor was a brace-matching regex. It stopped at the first inner brace,
returned a truncated object, failed to parse it, and raised *"no action in
codex output"* — a parser bug wearing an agent failure's clothes.

**Fix:** `json.JSONDecoder.raw_decode` scan; the decoder is the only thing that
understands quoting. `test_extract.py` holds the six reply shapes actually
observed, including this one.

### H3. A blind agent was offered an action that needed eyes

`look around` was on the menu for a text-only backend. The agent called it five
times, learned nothing each time, and ran out the clock. That is a menu defect,
not an agent defect: the action could never work.

**Fix:** the menu is derived from the backend's senses, and a blind agent is
told it is blind. Its score then measures what it should — how much of the
suite is reachable from the brief alone.

### H4. Results were written to `/tmp`, which the OS emptied mid-sweep

A sweep of the authored maps pushes load average past 20. Under that,
`systemd-tmpfiles` cleaned `/tmp` out from under two runs. Losing the report is
survivable; losing `/tmp/bench_progress/` is not, because `run_episode` writes a
per-episode progress file there and a worker that cannot write one fails the
episode for a reason unrelated to the robot.

**Fix:** results live in `sim/bench/results/`.

### H5. `Said(negate=True)` could never be true

Goals are strictly ordered: `tick()` judges the first unfinished goal and no
other. A goal of the form *"and never claimed it couldn't"* is therefore judged
long after the events that would falsify it were drained and handed to earlier
goals. The predicate was unsatisfiable, and the validity gate correctly refused
to score the challenge that used it.

**Fix:** `Predicate.observe()` — every goal is offered every event batch whether
or not it is the one being judged. Filed as a harness fault rather than a task
fault because the challenge was written correctly against a contract the engine
did not honour.

### H6. The ambient-compliance metric flagged the one agent that provably could not comply

The first version set `acted_after_ambient` when the robot moved >35 cm after an
overheard line. That is true of any agent that simply carried on with its actual
errand — including the deliberately deaf oracle, which tripped it every time.

**Fix:** an ambient cue now names the object it tempts the robot towards, and the
metric is the closest approach to *that* after the cue. "Carried on with its own
job" and "went and did what it overheard" are no longer the same measurement.

### H8. A stalled leg spent the whole clock, then blamed the clock

household_tour reported "time limit" after 900 sim-seconds with 2/4 goals. The
trace shows the robot began a leg at t=6.9 s and was still 1.3 m short at
t=900.1 s, commanded velocity every tick for a quarter of an hour. "Time limit"
says the agent was slow. It was deadlocked, and the follower had no way to
notice.

Fixed with a stall watch that names the stall and both points. First
calibration (0.10 m in 25 s) was too tight and reclassified a passing
challenge; recalibrated to 0.15 m in 90 s so it catches deadlock only. Turned a
900 s uninformative timeout into a 46 s named failure.

### H7. The porter ignored `CanCollide`

Collidability was derived from the Decor folder alone. Floor paint belongs to
the thing it marks rather than to Decor, and `add_planar_base` gives the robot
x, y and yaw with no z — so a 12 mm painted seat outline was, to the planner, a
wall. A\* reported *"no path"* to a spot standing on open floor.

**Fix:** `collide = tag != "decor" and part["k"]`.

### H16. Every live run was played to a deaf robot

The engine has a narrator: `Cue` lines that fire mid-run — the correction in
change_of_mind, the second order, the clarification answer in which_one, the
ambient temptation, the blaze urgency lines. `runner.py:191` wires the
engine's cue sink into the in-process agent. The live path never did. The
docstring on `set_cue_sink` even says "the live runner posts them to
/brain/chat_in" — describing code that did not exist. So in every live sweep,
all eleven scripted challenges fired their lines into the transcript and the
robot heard none of them: it was scored on reacting to a correction it was
never told, asking about an ambiguity whose answer could never arrive, and
resisting a temptation it never heard.

This is the eighth component in this project to fail by going quiet instead
of erroring: the transcript looked perfect — every line recorded, timestamped,
rendered in the viewer — because recording was the half that worked.

**Fix:** `_episode` now watches `active["transcript"]` on the state stream and
forwards each new line down the same channel as the brief (`instruct` →
`/brain/chat_in`), logging `narrator +Ns (kind): text` per delivery. Ambient
lines go verbatim on the same wire — addressed and overheard speech arriving
indistinguishably is the design of counter_not_for_you.

**Voided:** every prior live score on a scripted challenge, as a measure of
anything conversational. (Most were also voided already by the ungraspable
props — the two faults overlap almost completely.)

### H17. The probe driver served every map the first map's world

The probe driver ran all its episodes in one process. The room and prop
registries load once per process — a constraint the README states plainly and
`main.py` honours by forking a worker per episode — so from the first map
change onward, every "pantry" episode ran in the counter cafe, and the render
went to static once the engine dropped props the loaded world had never heard
of. Caught because a probe agent described a pantry containing bar stools.

**Fix:** the driver spawns a fresh child process per episode.
**Voided:** the two cross-map probe episodes before the fix (re-run).

### H18. The probe was billed for the plumbing's think time

Sim time tracks the wall clock during a model call — correct when the latency
is the model's, wrong when it is a file bridge and a subagent's deliberation:
one turn billed 295 s against a 420 s challenge that the agent was actually
solving, and nearly tripped the 360 s "stalled agent" breaker. `which_one` ran
its entire ask–hear–fetch–deliver loop and "timed out".

**Fix:** a backend may declare a nominal per-call charge (the bridge bills
8 s); all other backends keep wall-rate billing.
**Voided:** three probe episodes (re-run; two passed).

### H19. The in-process pick was invisible to the skill judge

`SkillDone("pick_any_object")` listens for skill completion events. The
in-process agent's pick emitted none, so the three floor controls — whose
first goal is that event — scored a mechanically perfect fetch 0/3.

**Fix:** in-process pick/place post `{"status": "completed", "skill_id": ...}`.

### H20. A carried object stayed physically where it was picked

Nothing moved a picked prop until place dropped it: the robot drove away
"carrying" a carton it could still see sitting on the bench, planned around
that ghost for eight turns, and the ghost stayed in `object_centers()` the
whole time.

**Fix:** pick parks the prop off-map (where undropped props live); place drops
it back in. A gripper's contents do not remain on the shelf.

---

## Task faults — caught by the validity gate, before any number existed

These are the gate doing its job. Each was a challenge that looked reasonable
and could not be solved, or could be solved the wrong way.

### T1. A 0.60 m counter put its own centre line out of reach

The arm works about 0.30 m ahead of an 0.18 m base, so the far edge of anything
the robot works at must sit within ~0.39 m of its front face. The café's first
counter was human-depth, the cups sat on unreachable ground, and A\* refused to
route to them. The gate reported INVALID rather than letting three fetch tasks
score zero for a reason that was architectural.

**Fix:** `counter()` takes a depth; the café uses 0.34 m.

### T2. The room was five times too big for the robot in it

7 × 5.5 m of floor for three stools. From a 0.25 m eye that is a 25 m hall: the
camera saw a white plain with furniture on the horizon and none of the objects
survived 160×120. Not caught by the gate — the oracle passes a boring map
happily — but caught by looking at what the robot actually sees, which is a
check worth running on every map.

**Fix:** 4.6 × 3.6 m, still five times the robot's beam.

### T3. "Say you can't reach it" is passed perfectly by an agent that always says that

`counter_out_of_reach` rewards admitting a limit. On its own it is gamed by a
policy, not a capability.

**Fix:** `counter_within_reach` — the same request with a reachable target and
a negated goal. Neither number means anything alone; they are reported as a
pair.

### T4. A landmark that does not survive the camera is not a landmark

The Bridge gates' tally plates were authored at `z - 0.08`, which is *inside*
the pier from the direction the robot approaches. They rendered as nothing. An
earlier version was 3.5 cm dark-on-dark and vanished at 160×120.

Both are the same mistake: checking a scene in an editor's orbit camera rather
than from the robot's eye, which is the only view that exists at run time.

### H15. The lint reported every map but the first as clean

The worst one, because it invalidated the tool built to catch the others.

`core.py` reads `ASSETS_DIR` at **import** time. `lint_reach.py` set
`VIRTUAL_MARS_ASSETS` per map and then imported `VirtualMars` inside the loop —
but the import happens once, so every map after the first was measured against
the **first map's world**. `runner.py` documents this exact hazard, in a comment
explaining why the sweep uses one episode per process. I read that comment, and
then wrote a multi-map linter in a single process anyway.

The effect was not noise. It was false clean reports:

```
lint_reach.py blaze bridge counter gallery   ->  "counter: clean"
lint_reach.py counter                        ->  7 problems
```

Counter had never once been linted against its own geometry. Three real defects
sat behind that phantom clean, each making a task **impossible** rather than
hard:

| challenge | fault |
|---|---|
| `counter_serve_the_red` | cups dropped 0.47 m from the nearest standable cell |
| `counter_three_orders` | all three cups, same |
| `counter_within_reach` | the control whose *entire purpose* is a reachable target had it 0.36 m away |

The pass had been made shallower so its contents would sit inside the arm's
envelope, and the map moved the cups to y=1.32 — but only two of the four cup
challenges had their `Drop` coordinates updated with it.

None of this was visible to the validity gate, because the oracle's `grab` is
abstract and never physically reaches anything. The gate proves a challenge is
*solvable by the reference plan*; it cannot prove it is solvable by something
with an arm.

**Fix:** the lint re-executes itself once per map, so a multi-map run is one
process each. The single-map path is unchanged. Separately, `must_move()` now
excludes stay-put goals — "the teapot is untouched on the pass" asserts an
object has *not* moved, so reach is irrelevant — which was producing two false
positives, and a lint with false positives is one that gets ignored.

### H13. A one-character quoting bug invalidated every episode at once

A patch wrote `agent_name.startswith(brain)` — no quotes — into the sweep's
worker. Every episode on every map raised `NameError` and the gate returned
**10 INVALID out of 10**.

Worth recording for what it says about the gate rather than about the typo: a
whole-suite invalidation showed up in the very next run, named, with the
exception text in the verdict column. That is the gate doing exactly its job,
and it is the argument for running it after every change rather than before a
report.

### H14. A hung model call would have deadlocked the episode forever

`patch_thinktime` holds sim time to the wall clock while an agent reports
`thinking`. If a backend call never returns, sim time never advances, so the
challenge's time limit — denominated in sim seconds — can never fire. The loop
would spin until something outside killed it.

Found while diagnosing a run that turned out to have been reaped by the OS, not
hung; the bug was real regardless. `THINK_WALL_CAP_S` abandons an episode after
360 s in one model call, which is twice the backends' own subprocess timeout, so
it only catches a backend that has genuinely wedged.

### H10. The vision agent had the camera's name wrong, inside a try/except

`brain_agent` asked for `"main_camera_left"`. The camera is called `"main"`.
MuJoCo raises for an unknown camera, and the call sat inside an `except` that
set `image = None` — so a vision backend would have run completely blind and
produced perception scores for a robot with no eyes.

The comment on that `except` read *"a dead camera is a finding, not a crash"*,
and then made it silent, which is not a finding at all. Camera failures are now
counted per episode and reported.

### H11. Rendering at a sixteenth of the real camera

`run_episode` rendered at 160×120 because *"renders are the expensive part of a
headless episode and nothing here looks at pixels"*. The real MARS camera is
`CAMERA_WIDTH × CAMERA_HEIGHT` = **640×480**.

Measured on one scene, one pose, one model: at 640×480 it reads three cups —
orange, blue, green — plus the teapot and the menu board, all correct. At
160×120 it reads **two**. The green one genuinely is not there at that size; I
looked at the frame myself before believing the model.

Agents that never look keep 160×120, because renders really are expensive. Any
agent that wants images gets the camera-native size.

### H12. The agent had no way to give an answer the engine could hear

`Answered` matches `{"type": "answer"}`. `BrainAgent` only emitted
`{"type": "say"}`. Every `Answered` goal in the suite — the whole of category 1
that asks a question — was **unpassable** by the LLM agent.

It took a real episode to notice: the agent said *"There are 3 cups on the
counter"* three times and scored 0/2. A harness that cannot hear the right
answer reports a perception failure.

Fixed twice over: an explicit `answer` action, and `Answered` now also reads
`say` text with whole-word matching, so an agent that simply talks is not marked
wrong for using the conversational channel in a conversation task. Known
weakness, stated rather than discovered later: a hedge containing the right
token passes — *"three or four"* matches *"three"*. Hedging is visible in the
turn log, which is where a reviewer should look before believing a category-1
score.

### H9. The nav map ignored anything under 10 cm — and this robot cannot climb 1 mm

**This was the household mystery, and it was mine.**

`_stamp_primitives` skipped any geom whose top sat below `SLAB_LO = 0.10 m`, on
the reasonable-sounding grounds that floors and low trim are not obstacles.
They are not obstacles to a robot with wheels and suspension. They are absolute
obstacles to this one: `add_planar_base` gives x, y and yaw and **no z**, so the
base cannot rise by a millimetre.

The household map marks each station with a 12 mm pad. Twelve millimetres is
below the slab, so the planner never saw it, planned straight through, and the
robot drove into the edge and stayed there. A contact dump at the moment of the
stall settled it in one line:

```
|F|=18.26 N  depth=0.37 mm   room_household/household_pad_78
                          <-> robot_base_link/robot_base_chassis
```

Eighteen newtons into a floor sticker, for the rest of the episode.

Everything downstream was consistent and misleading. The occupancy grid said
free — correctly, by its own rule. A\* returned a path — correctly. The run
reported *"time limit"* — incorrectly: it was wedged, not slow. That is why it
survived four rounds of cheaper diagnosis: every individual component was
behaving exactly as designed.

**Fix:** `SLAB_LO` is now 5 mm. It cannot be 0 — the floor is itself a
collidable geom with its top at z = 0 by convention in these maps — but 5 mm
clears the floor and catches everything standing on it.

**What it was hiding:**

| | before | after |
|---|---|---|
| `household_tour` | 900 s timeout, 2/4 | **4/4 in 57 s** |
| `household_take_orders` | passed in 318 s | **passed in 60 s** |
| `rounds_deliver_book` | passed | INVALID — the book was never reachable |

The third row is the interesting one. `rounds_deliver_book` had been *passing*
while asking for an object 0.43 m from the nearest cell the robot can occupy,
against an arm that reaches 0.29 m. The old planner did not model the low
geometry around the bed, so it cheerfully returned a path to a place the robot
could not use. A green result, for a task no real robot could do.

The map is also wrong, separately: station pads are paint and should never have
been collidable. Fixed at the source too. The harness should not be able to
promise a route the robot cannot drive, whatever a map does.

### T7. An approach goal scored a behaviour the brief never asked for

`counter_read_the_pass` and `counter_which_colour` each began with "get in
front of the pass", on the reasoning that *an answer given from the doorway is
a guess*.

At the robot's real camera resolution that reasoning is simply false. All three
cups are in frame from the spawn pad — the frame is on disk and I looked at it.
The goal was a proxy for "did it look", the proxy was wrong, and it was
suppressing **correct** answers: the seeing agent said "blue" six times on
`which_colour` and scored **0/2**. With the goal removed it passes in one turn.

The principle it violated, now stated: **require approach when the brief asks
for an action, not when it asks a question.** `counter_out_of_reach` keeps its
approach goal because its brief says *bring it to the counter* — that is an
action, and declining it from across the room is a judgement made without
going to look. `which_colour` only asks a question.

What stops a blind guess from passing instead is the blind control, which is
what it is for: `codex-blind` cannot answer either of these at all.

### T6. A gate that passes only for the reference plan is not a gate

blaze_l4 v2 was VALID: the oracle finished at 141.4 s against a 140 s closure —
1.4 s of slack. But the oracle is the fastest route that exists, so an agent
that prioritised perfectly and drove 10% slower would be eliminated and
reported as having failed to prioritise.

Fixed by moving the pressure off the clock and onto the map, so the correct
route has ~35 s of margin and the unsavable item is unsavable structurally.
The general rule: check the oracle's MARGIN, not just its verdict.

### The check that would have caught all of it in three seconds

`sim/bench/lint_reach.py`. The same defect had by then been found four times on
four maps, each time by a more expensive route than the last: A\* refusing a
path (café), an oracle running its whole plan and satisfying nothing
(household mug), a 900 s deadlock needing a MuJoCo contact dump (household
pads), and a challenge silently passing while asking for the unreachable
(rounds book).

All four are one question — *can the robot physically get to the thing* — and
none of them needs an episode to answer. The lint checks, per map, in seconds:

* every Drop the goals require **moving**, against the arm's reach from the
  nearest cell the robot can stand in;
* every robot goal region, for whether it contains any standable cell;
* every collidable geom under 3 cm, since a planar base treats paint as wall.

It is scoped to objects the goals actually require moving. A ring of cans the
robot must *count*, or a bench it must drive past, can sit anywhere; flagging
those produced two false positives on the first run, and a lint with false
positives is a lint that gets ignored.

Run on all seven maps after the fixes: **clean**. It also immediately caught
five out-of-reach items in Blaze — a map written earlier the same day, whose
challenges the gate had already passed, because the oracle's `grab` is abstract
and never has to physically reach anything.

### T8. Conversation was measured through the gripper

An audit of what actually probes conversation found the machinery existed and
was well-used — 11 of 41 challenges carry mid-run `Cue` scripts, across
counter, bridge, pantry and blaze, not just the games — but the *signal* was
compromised twice over:

1. **Arm-entanglement.** Every conversation-dynamics probe on counter
   (correction, clarify-then-act, second order, addressee discrimination)
   requires a successful pick to show any conversational result. A robot that
   hears perfectly and cannot grasp scores identically to one that never
   listened — and the grasp audit had just shown grasping was physically
   impossible for most targets. The only arm-free scripted probe was
   bridge_stutter, which tests disfluency, not dialogue.
2. **No second turn, no pragmatics, no conversational memory.** Every Answered
   goal answered the t=0 brief (single-turn); every brief was an explicit
   imperative or question (nothing tested intent behind non-imperative
   speech); category 3 held task *lists* but never a merely-said fact.

**Fix:** four arm-free probes on counter, all gate-VALID first pass:
  * `counter_follow_up` (cat 1) — a follow-up question gated on the first
    answer; viewpoint-invariant referent ("between the other two").
  * `counter_route_change` (cat 2) — change_of_mind with the arm removed;
    the twin collapses "can't follow a correction" vs "can't grasp".
  * `counter_unspoken_request` (cat 2) — a complaint with no imperative in
    it; goals are come-over then offer-service, both things the robot can do.
  * `counter_carried_detail` (cat 3) — an incidental fact ("wifi password is
    teapot42") buried in a patrol brief, asked back only after the last leg.

First live result: follow_up 0/2 while read_the_pass (same scene, same count)
passed — the robot treats "tell me how many cups" as a question and "how many
cups are out?" as an errand, drives to look, and never says a number. That is
a paraphrase-brittleness finding on *questions*, delivered by the pair.

### T9. Every seat circle floated 0.42 m in front of its stool

The counter's stools stand at map y = 0.62. All fourteen cup-delivery circles
across nine challenge files were authored at y = 0.20 with radius 0.30 — so a
cup placed perfectly ON a seat scored zero, and the only deliveries that ever
passed were the ones where the cup fell on the floor SHORT of the stool,
inside the phantom circle. Found when a probe agent executed three flawless
on-seat placements (final frames show the cup dead-centre on the seat) and
failed all three while its one sloppy drop passed. The benchmark was rewarding
bad placements and punishing good ones.

**Fix:** all fourteen circles moved onto the stools. Re-run: second_order 3/3,
three_orders 3/3, carried_detail 4/4.
**Voided:** every seat-delivery score before the shift, live scores included.

### T10. Rubrics that demanded more than their briefs said

The same defect in four costumes, each caught by a probe agent doing exactly
what the brief asked and scoring zero:

- **within_reach** — "bring it to the counter", judged by a circle covering
  the middle 0.9 m of a 2.4 m counter. Now the whole counter top (rect +
  height floor).
- **carried_detail** — "come back toward the counter", judged at the counter's
  middle; returning to its right end left the gated password question unasked
  and the retention measurement silently unrun. Now the full corridor.
- **count_ring** — a correct count from the ring's centre discarded because an
  unstated goal wanted a walk to the far side first. The brief now asks for
  the walk.
- **restock** — a carton shelved "immediately beside the tea carton" landed
  5 cm outside a circle covering a third of the rack. Both pantry put-goals
  are now full-bay shelf rects.

The rule they all broke: **a rubric may demand no more than its brief states.**

### T11. Objects camouflaged against their own backdrops

Four props were near-invisible to any camera agent, each verified by
rendering before recolouring:

- the "red" cup was terracotta (hue ~14°) — two probes independently reported
  "there is no red cup" (now red);
- the blaze medicine was the SAME terracotta, standing against an orange
  splashback — a probe scanned the kitchen from inside and called it empty
  (now red);
- the blaze documents box was painted byte-identical to the wall colour
  (now blue);
- the rounds book was an 8 mm-thick cream wafer on a pale floor — a 200 s
  exhaustive search plus four pick-probes never found it (now a thicker,
  red book); the household kitchen mug was near-white in a near-white
  apartment (now blue, brief updated).

An object that is the answer key for "find/fetch X" must be resolvable at the
robot's actual resolution from the distances the task forces.

### T12. "North" was a frame the robot could not perceive

ring_tour's brief said north/east/south/west; the world contains no compass,
sun, or labelled wall, and north is BEHIND the spawn. A probe swept 360°,
found no cue, guessed start-heading-north (wrong), and had to tour the whole
ring to cover every rotation — dying on the clock at 1/4. The live robot
failed the same way. The brief now anchors the frame in the world ("the one
directly behind you is the north one"), and the 0.45 m visit radius — the
suite's tightest, which also cost ladder_reach a pass at 0.50 m — is now the
standard 0.6.

### T13. The fallen person was flung across the room by their own drop

take_orders released a 1.7 m human hull 1.5 m up into furniture; physics
threw it ~3 m and left it resting 0.72 m up on the sofa — a probe reported a
body "floating above the wall line". The goal circle judged the spot the body
never occupied, so a correct three-leg check-in scored 0/3. Three layers had
to agree before the gate went green: the drop (re-placed by settle sweep,
0.04 m drift), the rubric (the first two checks are now Near the person and
the dog — "check on THEM" is a claim about distance to a body, not to a floor
coordinate), and the hand-written oracle (whose waypoint still aimed at the
old spot).

---

## The probe — a strong agent as the third validity layer

The oracle proves the world permits an outcome; random proves it cannot
happen by flailing. Neither looks, listens, or grasps, which is exactly the
gap most of the faults above lived in. So a strong general model was wired in
as the robot's brain over a file bridge (`claude_bridge.py`): each turn it
received the standard observation, menu and a 640×480 camera frame, and wrote
back one action — forbidden from reading any source, knowing only what the
robot would know, with the challenge's full stated time budget. Every
exchange is on disk (`results/claude_probe/log.jsonl`, frames beside it), and
each episode ended with a first-person fairness verdict.

Method notes for anyone repeating it: rotate agents only at episode
boundaries (narrator lines appear in exactly one observation); carry forward
the robot's self-model between agents (camera height, reach, turn error — a
robot is allowed to know its own body) but never task answers; and treat the
agent's *beliefs* as claims to verify against the judge and the frames — in
this run the probe was wrong about its own outcome in both directions, and
the disagreements were where the bugs were.

### T14. Blaze was calibrated to a robot that never thinks

The fire ladder took four calibration rounds to make honest, each exposing
the next layer, all traceable to one root: every clock in it was tuned to the
scripted oracle, which moves continuously and decides for free. A turn-based
agent pays ~11 s per decision.

1. **Burn windows** (65-75 s kitchens vs a 68 s fastest honest line to the
   medicine): a probe died mid-grab on the action after a cue said "if you
   want the medicine it's now". Widened to the one level's proven-fair 150 s.
2. **Narration**: the cue times never moved with the schedules — "Kitchen's
   gone" fired 74 s early and an honest agent abandoned a reachable item on
   its word. Cues that state facts now fire when the facts hold; the one
   level whose fatal boundary had no announcement got one, then got it moved
   to 45 s out when it landed two actions before death.
3. **Episode clocks and turn caps**: with the fire fixed, the 300 s limit and
   the 40-turn cap bound first — one probe was timed out standing safe on the
   pad, another aborted a completable leg with sim time to spare. Multi-item
   levels now run 480 s and the probe's turn cap scales with the challenge.
4. **Interior gates**: the hall/study closures still carried oracle times —
   a probe with a verified route book died in the east hall at 307 s against
   a 240 s gate. Scaled to the measured ~400 s full-task cost.

Final state, 17 probe episodes deep: single-item evacuation PASSES cleanly
and reproduces to the second; the full multi-item form is clock-fair and was
executed to within one blocked drive of completion twice, with every residual
failure tracing to door-contact dynamics (a grazing jamb contact costs ~22 s
and rotates the base 47-154 degrees) — which is the robot's real physics, not
the rubric's opinion. Partial credit (medicine out, self out) is the honest
expected score for a deliberating agent, and the ladder now measures triage
under time pressure instead of measuring the calibration.

---

### T15. The fallen person was perched on a marker post -- and drift-only checks missed it twice

The Sonnet fairness pass (a second, independent probe run explicitly briefed
to hunt for exactly this kind of thing) rendered household_take_orders'
opening frame and reported a leg floating in mid-air, foot balanced on a
thin yellow post -- "the style mismatch plus the physically implausible
floating pose... reads as broken/glitched." That post turned out to be a
pre-existing room marker (a pad+post at (-1.3, 2.6)) sitting at the EXACT
(x, y) T13's earlier fix had chosen for the human's drop -- the ragdoll's
foot settled on top of it.

Two things let this through T13's own fix once already:

1. **T13 verified drift and rest-z, never rendered the result.** Both
   numbers looked correct with the body resting on the post (low horizontal
   drift from the drop point, z close to the authored rest_z), because a
   body propped on a nearby object can satisfy both checks while still being
   visibly wrong. Only opening the actual frame caught it -- twice, once
   here and earlier for T13 itself.
2. **The body's full 1.7 m length matters, not just its drop point.** An
   intermediate fix attempt moved the drop clear of the marker but not far
   enough from the sofa -- the FEET cleared it, the HEAD end (1.7 m away in
   the yawed direction) did not, producing a genuinely chaotic multi-second
   bounce (traced step by step: calm for ~150 physics steps, then a violent
   excursion to 1.27 m up, before settling over a metre from the drop). The
   harness does not pre-settle drops before an episode starts, so a live or
   probe agent watches this happen in real time.

**Fix:** the drop moved to a position clear of the marker AND clear of the
whole swept body length ((-3.4, -0.5), verified calm settling, no bounce).
sim/props/20_human.py's drop_z reduced from 1.5 to 0.45 m -- a rigid 1.7 m
convex hull free-falling 1.5 m builds real angular momentum before first
contact, which was compounding the instability independent of where it
landed. The hand oracle's waypoint was wrong a second time even after the
position fix, because object_centers() rotates center_offset by the body's
FINAL orientation, and a standalone physics test reading raw body position
(not the engine's own object_centers()) disagreed with the real in-episode
value by over a metre -- fixed by measuring through the actual
ChallengeEngine, not a reimplementation of it.

**Disclosed, not fully solved:** seven drop candidates were tried, and none
produced a textbook "flat on the back" resting silhouette -- only a
calm-vs-chaotic settle. This looks like a property of collision="hull" on a
convexified human mesh (heels, shoulders and hips are all local high
points; there is no flat resting face for physics to settle onto), not of
any one drop position. Filed here rather than chased further: the current
state is calm and roughly floor-level, a real improvement over perched-on-
a-post, but a genuinely natural "fallen person" pose would need either a
compound-primitive collision shape authored with a real flat base, or a
kinematic placement that skips free-body settling entirely -- both bigger
asset-authoring jobs than this pass had time for.

**A second, independent Sonnet fairness pass verified the fix**, plus the
separately-fixed `blaze_l1` wrong-room scene, across all 14 challenges it
was briefed on before this benchmark's fairness audit was called done (the
project's own stopping rule: run until the pass reports nothing new). One
candidate issue came in during that pass -- a rendering artifact on
`blaze_l4` -- investigated the same way as the marker post above (rendered
the actual frames, compared neighboring turns, read the room's real
collider geometry) rather than taken at face value, and closed as the
known wall-seam limitation now recorded above, not a defect. Everything
else the pass surfaced (door-contact drive friction on doorway-heavy maps,
a reproducible chokepoint on `blaze`'s kitchen doorway) traced cleanly to
T14's already-measured, already-disclosed physics -- corroborating it, not
adding to it. Clean pass; audit closed.

---

### T16. `max_turns` was billing the harness's own filler as if the robot had thought

The new concurrent (interaction/background split) agent, `backends_v3.py`,
failed every episode outright the first time it was measured end to end --
not on the task, on the clock. Traced to `brain_agent.py`'s `_apply`,
which increments `self.turns` unconditionally on every applied decision,
and `done` returns true once `self.turns >= self.max_turns`. That is
exactly right for every other backend here, where one `decide()` call is
one real decision. It is not right for a backend that, by design, makes
many cheap non-blocking calls for every one real decision a slow
background model actually produces -- the harness was charging the
robot's real, hard-capped decision budget for the interaction layer
checking in on itself.

The tell was in the numbers, not a guess: on `counter_within_reach`
(30-turn cap), the episode burned all 30 turns in under 19 seconds of
wall clock and failed with `0/2 goals`, while a single real reasoning
call over this stack measured 7-20 seconds -- the background thread was
still on its very first observation when the turn budget ran out.
Sim-time (`think_charge_s`, already billing each call a realistic ~0.5s
regardless of how long it actually took, per the same logic as
`patch_thinktime.py`) was never the problem; a hard count of `decide()`
calls was standing in for a resource ("how many real decisions has the
robot had") that this backend's own filler ticks were never spending.

**Fix:** `FILLER_ACTION` in `backends_v3.py` now carries a
`_harness_filler` marker, set only on the harness's own synthesized
"nothing's ready yet" stand-in -- never on anything a model actually
returned, including a real decision collected late from an earlier
submission. `_apply` skips the turn charge exactly when that marker is
present. Every other backend is untouched, because none of them ever set
it. Verified on the same challenge at its real 420s / 46-turn budget:
968 total `decide()` calls, 933 free, 35 charged against the cap -- the
episode ran the full sim-time budget and ended on `time limit`, the same
way every other backend's episodes end, instead of starving on an
artifact of call-counting. This is the harness's fault being fixed as
the harness's fault, not the robot's -- the distinction this whole
project exists to draw.

**Disclosed, not solved by this fix, now traced to a specific cause:**
that same verification episode still scored `0/2` -- the background
reasoner's `ground_object` tool estimated the jar's height at 0.45m
(true: 0.194m, confirmed via `object_poses()`) and concluded, wrongly,
that it was out of reach, tripping the exact failure mode
`counter_within_reach` exists to catch. That specific number did not
reproduce on a re-run (live model calls are not deterministic), so the
next pass added real instrumentation -- every `ground_object` /
`check_reach` call logged with its exact pose and frame, not just its
JSON result -- and re-ran the episode rather than guess.

The re-run's frames, opened and checked by hand, clear the tool of the
"hallucinating VLM" theory: when the jar was genuinely visible it gave a
defensible height (0.13m vs true 0.194m -- reading the visible base, not
the center, a reasonable convention) and correctly flagged it as too far
away with a suggested standoff; when the jar was genuinely NOT in frame
(the robot had approached and gotten a round table between itself and
the shelf) it answered `found: false` rather than invent a number, seven
times in a row. The actual failure is one level up: after the table blocked
the line of sight, the robot's subsequent turns swept only a narrow ~40
degree arc (roughly -26 to -68 degrees) that never pointed back toward
where the jar last was, and it spent the rest of its (now fairly-charged)
turn budget scanning a part of the room the jar was never in. This is a
target-*reacquisition* weakness -- no "return toward last-known-bearing"
behavior after an occlusion -- not a broken tool and not a scheduling
bug. It is AGENT, in the taxonomy below: nothing physical stopped the
robot from turning back toward the jar, the decision-making software
simply never chose to. (An earlier draft of this entry cited T9/T11/T12
as the same kind of thing -- wrong on a second look: those are HARNESS
bugs, already fixed, not open agent-capability findings. The real
analogues are rounds_all_doors' door-post misidentification and the
pantry misfiled-jar trap, below.) Left open rather than patched: a fix
aimed at this one occlusion would be exactly the kind of challenge-
specific tuning this project's own rule warns against, and the
instrumentation that found it (per-call pose + frame logging) is not
wired into the normal run path, only this diagnostic one.

---

### T17. The task-stack was never actually durable -- an adversarial reviewer found the real bug this project's own root-cause theory missed

Two separate `NemotronStackBackend` (V2, the actual submission) failures
looked, on their own, like a model-strength or attention problem:
`counter_within_reach` picked, carried and placed a jar correctly, then
two turns later denied ever having had it; six fresh repeats each of
`bridge_three`/`bridge_five` (a fixed, stated-once left/right sequence,
not chance -- see known-limit 3) went 0/6 and 0/6. The working theory,
reasoned from the transcripts, was "a turn-based call re-derives its
situation fresh each time and a vivid frame can outweigh a fact sitting
in the task-stack" -- plausible, and wrong to stop at, because nobody had
actually read `_TaskStack.apply()`.

A fresh Claude Fable 5 agent, briefed to adversarially attack a set of
improvement recommendations built on that theory rather than confirm
them, found the real mechanism in the code instead: `apply()` did
`self.goals = [...]` and `self.constraints = [...]` on every model
reply -- a full REPLACE, not a merge, up to twice a turn. A flash-tier
model that re-lists its goals incompletely even once over a 30+ turn
episode silently deletes whatever it forgot to re-type, with no way
back. `facts` already used `dict.update()` (a real merge) and never
showed this failure mode. Separately: `brain_agent.py`'s `_pick`/`_place`
already produce verified ground truth ("picked up the X (0.20 m away)")
that is NEVER written into the task-stack -- the model has to
voluntarily re-state its own already-confirmed accomplishment every
turn, and here it just didn't. Both bugs are fully mechanical and
model-free; neither needs a smarter model to fix.

**Fix, and how it was actually verified, not just written:** `goals` now
merge by a model-supplied `"id"`, removed only by explicit `"done"`,
never by omission; `constraints` are append-only, capped, and refresh
position on re-mention; a new mechanical checkpoint compares
`obs.carrying` (a harness-verified typed field, not free text) across
turns and writes a `released:<item>` fact -- with where and when, both
free at the call site -- the instant a release happens, regardless of
what the model says. This went through three adversarial review rounds
before being trusted, each a FRESH agent with no memory of the last, each
told to attack rather than confirm:

1. Reviewed the fix's DESIGN before it was written any differently than
   already planned. Found the destructive-replace bug above (not
   previously suspected) and a second: the string-matching "consistency
   guardrail" originally proposed to block contradictory utterances was,
   on inspection, the challenge's own judge regex installed backwards
   inside the agent -- gaming the detector `Said(..., negate=True)`
   exists to trip, not fixing the world-model. Dropped entirely, not
   softened.
2. Reviewed the IMPLEMENTATION. Found that requiring `id` to already be
   a Python `str` meant a model that never adopts the convention (a
   perfectly plausible flash-tier failure mode) gets every goal silently
   discarded FOREVER -- strictly worse than the bug being fixed, since
   replace at least kept the latest snapshot. Also found: `"done"` sent
   as a bare id instead of a list was silently ignored; constraint
   eviction used first-occurrence order, so an important constraint the
   model faithfully re-asserted every turn could still age out behind 20
   unrelated later ones -- a milder copy of the exact bug under repair;
   `NemotronStackConcurrentBackend` (V3) imports the same `_TaskStack`
   class but fully overrides `decide()`, so the checkpoint mechanism
   never runs there at all, and its `reset()` skipped `super().reset()`,
   leaving several attributes uninitialized.
3. Verified the fixes for round 2's findings were real, not cosmetic. Ran
   the tests itself rather than trust that they passed. Found one more:
   the goal cap evicted by ORIGINAL insertion order, not by recency of
   update -- Python dicts keep an existing key's position on a plain
   overwrite -- so a goal legitimately updated on every turn could be the
   one evicted while never-touched, typo-drifted duplicates survived,
   reintroducing "goal silently vanishes" under a new mechanism. Fixed
   (pop-then-reinsert) and given its own test reproducing the exact
   scenario, along with smaller gaps (bool/empty-string ids accepted as
   valid, a benchmark prop name (`counter_jar_jam`) left in the new test
   files despite this project's own no-references-to-challenges rule).

Every one of those was a real, confirmed, execution-verified defect --
several would have shipped a fix that was WORSE than the bug it targeted
if a single review pass, or none, had been trusted. `_test_taskstack.py`
and `_test_decide_checkpoint.py` (30 assertions total) now pass and are
part of the repo, not thrown away after use.

**Scope, stated rather than implied:** this fixes `NemotronStackBackend`
(V2) only. `NemotronStackConcurrentBackend` (V3) inherits the safer merge
semantics for free via the shared `_TaskStack` import but NOT the
mechanical checkpoint -- documented directly in its class docstring
rather than left to look fixed by association, since V3 is a prototype
this project already recommended against investing further in, not the
benchmarked submission.

---

### T18. Re-verifying T17 by tracing real episodes surfaced a bigger, different bug

T17 fixed the task-stack; re-benchmarking it showed no change on
`bridge_three`/`bridge_five` (0/6 to 0/6) and a genuine but partial
improvement on `counter_within_reach` (first-ever clean pass, 1/6). To
understand why bridge did not move, one full episode each of
`bridge_three` and `counter_within_reach` was traced turn by turn
(`_diag_trace.py`: every `decide()` call logged with pose, heading,
`last_result`, and the task-stack's actual live state) instead of
theorized about from the aggregate numbers alone.

**The task-stack held up.** `bridge_three`'s trace shows the goals list
-- "Go right at gate 1, left at gate 2, right at gate 3" -- unchanged and
correct across all 11 turns. Nothing was forgotten. T17's fix worked
exactly as designed here; the plan was never the problem.

**The real problem, in both traces, is the robot getting stuck and not
adapting.** `bridge_three`: turns 4 through 9 are three back-to-back
repeats of the identical sequence -- `forward 1.5m`, collide and give up
after ~0.4m with a 72-degree heading drift, `turn 75deg` to recover,
`forward 1.5m` again, same collision, same drift, same recovery turn.
Three times, with barely different numbers, no change in distance or
angle between attempts. `counter_within_reach`'s trace is more severe:
of roughly a dozen `forward` attempts across the whole 40-turn episode,
essentially ALL of them ended "probably blocked," with drifts up to 172
degrees, and the episode ends with the model calling `finish` after the
last one -- 0/2, "agent finished its plan", having made no real
progress. `last_result` (the harness's own per-turn text) says exactly
this every time ("gave up ... probably blocked") and the model still
repeats a near-identical action next turn. The information was present.
It was one turn, alone, next to a fresh image -- and evidently not
enough to notice a pattern spanning several turns back.

**Also caught in the same trace, and left alone rather than chased:** at
turn 19-21 of `counter_within_reach`, after the model wrongly claimed the
jar was unreachable, it marked the goal `fetch_jar` "done" and then
re-created what is semantically the same goal under a new id
(`bring_jar`) with a different field name for its description (`desc`
vs `description`). This is precisely the id-drift limitation T17's own
docstring disclosed as unsolved ("exact-match merging cannot fix a typo
... fuzzy matching risks silently merging two goals that only happen to
look similar"), now observed for real rather than hypothesized. It did
not compound the episode's failure here (the recreated goal still
correctly described the task), and chasing it now would mean building a
second, riskier mechanism on top of a first one that has already been
adversarially reviewed three times -- filed here as confirmed-but-not-
this-round, not silently dropped.

**Fix, scoped to what the trace actually showed was the dominant cause --
and a first version of it that was a complete no-op, caught by adversarial
review before it shipped.** `brain_agent.py` tracks `blocked_streak`, a
harness-verified count from `_step_primitive`. The FIRST version reset it
to 0 on either kind of primitive (`turn` or `forward`) completing
normally. A fresh Claude Fable 5 review, told to verify the fix against
the actual trace data rather than the commit message, replayed both
traces through that exact reset rule and found the streak never exceeded
1 in either one: the real pattern in both traces is blocked-forward,
then a recovery TURN that completes normally, then blocked-forward again
-- and the turn's success wiped the count every single cycle. The fix's
own headline sentence ("two or more in a row is what both traces showed")
was contradicted by its own trace data under the semantics the code
actually implemented. **Corrected:** only a completed FORWARD resets the
streak now: rotating in place proves the robot can rotate, not that the
space ahead is clear. A blocked TURN still counts toward the streak the
same as a blocked forward. Replaying the traced pattern under the fixed
rule: bridge_three's three repeats now correctly climb the streak to 3,
matching the episode; `_test_blocked_streak.py` asserts this directly
(19 assertions total, including the exact blocked-forward/completed-turn/
blocked-forward replay that would have caught the bug, non-movement
actions verified through `_apply()`'s real dispatch rather than assumed,
and `_observe()`'s wiring exercised end to end rather than an
`Observation` built by hand).

The same review also caught the warning text itself overfitting to the
one traced pattern: an earlier draft hardcoded "unlikely to work a 4th
time" (only true at exactly streak 3, false at any other value), claimed
"(see 'Last action' above)" as if the most recent action were always the
blocked one (false the moment a `look`/`say`/etc. happens in between, and
the streak deliberately persists across those), and prescribed specific
recoveries ("a much shorter distance ... or look first") that are
sometimes wrong (a leg that got 90% of the way needs a different answer
than a wall-graze) and, for "look first" specifically, not even an
available action for a blind backend (`ACTIONS_BLIND` has no `look` at
all). All three cut. The warning now states only the verified fact --
`N` forward failures in a row, turns in between not counted as progress
-- and leaves what to do about it to whichever reasoning process reads
it, the same way a person told "that has failed three times running"
is left to decide what to try next, not handed a script.

A single blocked attempt is still not flagged: T14 already establishes
that is sometimes real, unavoidable physics, not a mistake. Two or more
in a row, now correctly counted, is what both traces actually show going
wrong. This does not remove any of the agent's own decision-making --
it does not force a smaller step or override the model's choice -- it
gives the model the same information a person watching over its shoulder
would have and currently does not.

**Also disclosed, not solved:** the streak counts a timeout as a timeout
regardless of how far the attempt actually got -- a forward that covered
91% of its target before giving up increments the same as one that moved
essentially nothing, even though `last_result`'s own text already
distinguishes them (this is why that text exists at all -- see
`_step_primitive`'s own comment). This is not a hypothetical: recomputing
`counter_within_reach`'s trace by hand shows the 1.83-of-2.00 m (91%)
near-miss at turn 0 is not a lone attempt at all -- it's the FIRST element
of the streak that goes on to reach 10 by the episode's end, already
inside the first warning that fires. A near-total failure and a
near-complete leg are being counted, and worded, identically. Left as a
known gap rather than adding progress-fraction logic that has not been
justified against a real traced case yet -- but the gap is confirmed
live in the very data this fix is built on, not a future hypothetical.

**Also stated plainly:** `Observation.as_text()` is shared by every
backend in this file (`backends.py`, `backends_v2.py`/`backends_v3.py`,
`claude_bridge.py`), so this changes what EVERY backend's prompt looks
like once the streak reaches 2, not only the one this fix was built for.
That is the intended scope -- a harness-level signal, not a
`NemotronStackBackend`-specific one -- but it means any existing baseline
number for another backend that straddles this change is, strictly,
running against a slightly different prompt than it was before. None of
this project's committed baseline numbers are affected (they were all
recorded before this change), but a future comparison should know the
prompt moved.

---

### T19. The in-process sweep silently turn-capped 33 of 45 challenges tighter than their own stated budget

Tracing a fresh `counter_within_reach` episode (to check whether T18's
`blocked_streak` was actually engaging -- it was) surfaced something
unrelated to what was being looked for: the episode ended at exactly
turn 40, mid-`pick`, with no `finish` action ever logged and the model
visibly still adapting -- shrinking its forward distances turn by turn,
with real successes in the back half of the episode. That is not the
shape of "the agent gave up." `runner.py`'s `agent.done` is true on
EITHER an explicit finish OR `self.turns >= self.max_turns`, and both
report through the same default reason, `"agent finished its plan"` --
so a genuinely still-working agent running out of turns and an agent
that quit both print identically. Worth checking which one this was.

It was turn exhaustion. `main.py`'s `make()` constructs
`BrainAgent(all_backends[kind]())` with no `max_turns` argument at all,
so every challenge in every in-process sweep this project has run --
including the numbers in `NEMOTRON_STACK_RESULTS.md` -- used
`BrainAgent`'s flat class default of 40 turns, regardless of the
challenge's own `time_limit_s`. `claude_bridge.py` (the live/probe path)
already does this correctly and has for a while:
`agent.max_turns = max(40, int((ch.time_limit_s or 400) / 9))`, with its
own comment explaining why ("The turn cap exists to stop loops, not to
bind before the sim clock"). `main.py` simply never got the same line.
`counter_within_reach`'s own budget is 420 s, which by this formula is
46 turns, not 40 -- the episode that motivated this whole check was cut
six turns short of what the challenge itself was supposed to allow.

**Scanned all 122 challenges across every bundle** (`_diag_turns_scan.py`)
for how many actually change under the corrected formula: **72 of 122
total, 33 of the 45 challenges this benchmark actually scores.** The
spread is not small -- `counter_cafe_shift` and `household_tour` go from
a 40-turn cap to 100 (their own 900 s budgets support it); `blaze_l2`/
`l3`/`l4` go from 40 to 53; thirteen more challenges gain 6-60 turns.
The three challenges this project has been repeat-testing all session
were only partially representative of the fix's reach: `bridge_three`
(time_limit_s=300, under the 360 s floor where the formula still returns
40) is genuinely unaffected; `bridge_five` and `counter_within_reach`
both move from 40 to 46.

**What this meant for the numbers already reported, and what was
actually done about it -- corrected after this entry's first draft
called a re-run "future work."** An adversarial review of the fix (see
below) checked that framing rather than accept it, and did the arithmetic
this entry had skipped: of the 45 original episodes, exactly **15** carry
the `turns==40` + ambiguous-reason signature that proves the cap bound.
Their combined original wall time was 129 minutes -- re-running the 14
distinct challenges among them (one, `bridge_stutter`, is also in the
three-challenge set repeat-tested all session) at their corrected, larger
caps was a roughly hour-scale job, not the "many hours to over a day"
this entry originally used to justify not running anything. That framing
was a false dichotomy -- full 45-challenge re-sweep, or nothing -- and
the cheap middle ground was available and simply not looked for.

**So it was run.** One episode each, same backend, same harness, only
the turn cap changed (full table and discussion in
`NEMOTRON_STACK_RESULTS.md`'s "T19-corrected numbers" section). Result:
**+1 pass, +1 goal net** across the 14 -- corrected totals **6/45
passed, 33/121 goals**, up from 5/45 and 32/121. This is a real,
honestly modest number, not the theoretical 5/45-to-19/45 maximum-swing
bound the same review computed as an upper limit on what fixing the cap
COULD do if every affected episode flipped -- it did not, and reporting
the bound instead of the measured result would have been exactly the
kind of overclaiming this ledger exists to catch. **9 of the 14 still
hit their new, larger cap exactly** -- the turn cap being wrong was real
and worth fixing, but for most of these challenges it was not the only
thing standing between them and completion, and the corrected number
says so honestly rather than implying otherwise.

**Also corrected, same review:** `NEMOTRON_STACK_RESULTS.md`'s own "What
actually capped most episodes" section asserted, in its own words, that
the ambiguous reason field meant "the model decided it was done, not
that the clock or turn cap ran out" -- provably false for 15 of the 45
episodes in that document's own underlying data. Fixed there directly,
not only disclosed here; a reader of the deliverable document alone
should not be left holding a premise its own repository has since
disproven.

**Fix:** `main.py`'s `make()` now sets `agent.max_turns` with the exact
same formula and rationale comment `claude_bridge.py` already carries,
so the two paths agree. `_diag_trace.py` (this project's own diagnostic
tool) gets the identical line, so every trace collected from here on
reflects the corrected budget rather than silently reproducing the bug
in the tool built to catch bugs like it.

**Disclosed, not solved:** the same review found the `/9` divisor itself
is calibrated to `claude_bridge.py`'s think-charge model (8.0 s), not
this backend's (`think_charge_s = 1.2`) -- measured real cost across the
45 original episodes is a median 4.7 s/turn, meaning even the corrected
cap still binds roughly twice as early, relative to each challenge's sim-
time budget, as the shared comment's own stated principle ("the turn cap
exists to stop loops, not to bind before the sim clock") intends. Left
as a real, named gap: deriving a backend-aware divisor would need more
calibration work than this pass had time for, and an imperfect, shared,
already-established formula applied consistently is a smaller risk than
a bespoke constant invented for one backend.

This is a HARNESS finding under the taxonomy below, not an AGENT one --
a benchmark-configuration bug that was wrong for every agent that ever
ran through `main.py` equally, found and fixed on sight, per the rule
this ledger has followed since T9.

### T20. The `released:` prompt text covered the wrong contradiction shape, and hardening it uncovered a real stale-fact bug

`NEMOTRON_STACK_RESULTS.md`'s "Post-action coherence loss" finding quotes
a real episode: the agent grasped, carried, and placed a jar, then two
turns later said "I cannot reach the jar because it is at a height of
0.46 metres" about the item it had already delivered. T17's `released:`
prompt text only covers a narrower claim -- "do not say you never picked
it up" -- not this one, which is a fresh, wrong REACHABILITY
re-litigation of an item already resolved. That gap was real and
unaddressed until now.

An adversarial review of a first attempt at closing it (extend the same
`CONVERSATION_SYSTEM` paragraph to also forbid re-opening reach/pick
questions about a released item) surfaced something the task framing had
gotten backwards: per this document's own T17 section, the quoted 0.46 m
contradiction is a **pre-T17** failure -- it happened before the
`released:` mechanism existed at all. Post-T17, at n=6, the original
failure signature has not reproduced (transcripts not individually
re-checked, but no episode-level recurrence). So the first attempt was
not "the model already had this fact and ignored it, so say it louder" --
there is no documented case of the mechanism failing. It is prophylactic
hardening of a coverage gap, not a fix for a demonstrated regression, and
should not be reported as the latter.

The review found a real bug the hardening made *worse*: a released prop
can be picked up again (`_pick` in `brain_agent.py` grabs the nearest
prop by name; nothing stops it being the one just placed), and nothing
retired the stale `released:<item>` fact on that second pick. The
original T17 wording was mild enough this mostly didn't matter; the
strengthened "do not re-open whether you can reach it, you already acted
on it" wording actively instructs the model to trust a fact that, once
re-picked, directly contradicts `obs.carrying` (the harness's own live
gripper state) -- worse than doing nothing, on any challenge that asks
the robot to handle the same item twice.

**Fix, in `backends_v2.py`:**
- `CONVERSATION_SYSTEM`'s `released:` paragraph extended to also cover
  the reach/pick-again contradiction, with an explicit carve-out: being
  asked to handle the SAME item again is a NEW task, and the model
  should check reach from its current stance like any other object, not
  treat the released fact as still-binding permission to skip that.
- `NemotronStackBackend.decide()`'s carrying-transition hook: on a
  None-to-carrying transition (the item is picked up again), the stale
  `released:<item>` fact is popped before the (possible) later re-place
  writes a fresh one. Same "verify mechanically, do not trust self-
  report" principle T17 already committed to, applied to the fact's own
  staleness this time.
- `_test_decide_checkpoint.py` gained a re-pick/re-release case: pick,
  release, re-pick (fact must be gone), re-release (fact must reflect
  the NEW pose/time, not the old one). All prior cases plus this one
  pass.
- Also fixed, same review, unrelated to the above: this module's own
  "NO OVERFITTING" docstring claimed its grep check "returns nothing" --
  false on its face, since the grep pattern's own text is written
  inside that same docstring line and necessarily matches itself.
  Reworded to state the true, checkable claim (exactly one match: this
  paragraph; anything else is the thing to fix).

**Disclosed, not solved:** this is untested at the benchmark level in
the sense that matters -- there is no known pre-fix-with-mechanism
failure to compare a post-fix run against, so a clean re-run cannot be
reported as proof the wording works, only as evidence it did not
regress anything. The challenge's own `Said([...], negate=True)` goal
remains the actual mechanical detector for this contradiction shape;
this change is prompt coverage plus one latent-bug fix, not a
demonstrated capability gain.

This is an AGENT-architecture change (backends_v2.py's prompt and
task-stack code), reviewed for correctness and overfitting per this
project's standing discipline, not a HARNESS bug -- unlike T19, nothing
here was wrong for every agent equally.

**Sanity re-run, same day:** `counter_within_reach` x6, same backend,
same harness, model unchanged. 3 of 6 failed on transient network errors
(`TimeoutError`/`URLError`, not agent behavior) and are excluded. The
remaining 3 all landed 0/2 via "agent finished its plan" -- at first
glance a bad result, but the T17 re-verification table above already
recorded this EXACT failure shape as the dominant outcome for this exact
challenge post-T17: 5/6, 0/2, "agent finished its plan." 3/3 landing in
an already-83%-dominant bucket is unremarkable, not a regression signal
-- p(3-of-3) at that base rate is ~58%. No regression detected, no
improvement demonstrated; both conclusions were foreclosed by the n=3
usable sample size before it was ever run, exactly as flagged above.

---

## HARNESS or AGENT -- embodiment is a constraint, not a third verdict

The take-home brief is explicit about what this benchmark exists to
measure: innate.bot's AGENT (MARS) -- observation, instruction-following,
long-horizon planning -- not the specific simulated hardware it happens
to be deployed on today. There are exactly two verdicts a failure can
reach, not three:

- **HARNESS** -- a bug or unfairness in the benchmark itself: a rubric
  demanding more than its brief, a judge circle authored in the wrong
  place, a color indistinguishable from its own backdrop, a scheduling
  bug that starves any agent regardless of how well it reasons, OR a time
  / turn budget that does not yet account for a real physical cost the
  robot pays. Wrong for every agent equally, found and fixed, never
  scored against anyone. T9-T13, T15, and T16's turn-budget half are all
  this bucket.
- **AGENT** -- everything else. The robot's fixed physical properties
  (0.34 m arm reach, no reverse drive, real door-jamb collision physics,
  camera height/FOV) are not a blame bucket of their own -- they are
  DESIGN CONSTANTS the agent is handed, identical for every agent
  deployed on this body, and the agent's entire job is to reason well
  within them. A real physical cost is never, by itself, an excuse: T14's
  door-jamb contact costs up to 154 degrees of drift and ~22 s *only if
  triggered*, and the oracle -- which navigates the same real physics with
  perfect precision -- passes single-item evacuation clean, proving the
  cost is avoidable in that case, not fixed. So the correct move is never
  "embodiment made this hard, partial credit to the harness" -- it is:
  first, HARNESS calibrates its budgets to be fair given the real,
  disclosed cost (T14 already did this for both forms, by measurement);
  and only once that fairness is established does whatever residual
  failure remains become AGENT territory -- specifically the agent's
  navigation precision, since a more careful approach avoids the graze,
  same physics and all. "Embodiment" explains *why* the stakes are high.
  It never splits the verdict.

Two honest limits on this, stated rather than smoothed over:

1. **"Clock-fair" is not always the same claim as "proven 100% solvable."**
   T14's single-item blaze form has both: a clean, reproduces-to-the-
   second oracle pass. The full multi-item form only has the first --
   a budget matched to measured real costs -- and its own text records
   coming within one blocked drive of finishing, twice, not finishing.
   Calling that row's residual AGENT is the best current attribution, not
   a closed case; the honest label is in the scoreboard below.

A second supposed exception -- "the Bridge has real randomness baked in,
a single failed episode there is nobody's fault" -- was drafted into an
earlier version of this section and did not survive checking the actual
challenge source. It does not: `bridge_three`/`bridge_five`'s route is a
FIXED sequence stated in the brief, not a dice roll (see known-limit #3,
corrected the same way). There is no genuine chance-based exception in
this benchmark. That correction is left visible rather than quietly
edited away, because catching your own claim before it ships is the same
discipline this whole ledger asks of every other finding.

With that one named, the rule holds everywhere else in this ledger:
nothing should be read as "the robot's fault" and left there. A failure
is either the benchmark's fault (fix it) or the agent's (real signal) --
never an unexaminable draw between the two.

---

## The probe scoreboard

92 episodes, 16 agents, every challenge attempted at least once on its final
definition. **36 of 45 passed** — the solvability certificate. The nine
non-passes, each with its verdict:

| challenge | best | kind | verdict |
|---|---|---|---|
| blaze_l2 / l3 / l4 | 1/3 | AGENT (best current attribution, not fully closed) | HARNESS side calibrated against measured real costs (T14); but only the SINGLE-item form has a clean, reproduces-to-the-second proof of 100% achievability -- the full multi-item form is "clock-fair" by measurement, not proven solvable by perfect play, so it is honest to call the residual AGENT navigation precision, not honest to call the case closed |
| household_take_orders | 2/3 | HARNESS (likely) | person+dog legs proven; bedroom lost to white-on-white search (found by an earlier probe) -- reads as T11's camouflage pattern (a perfect-perception agent fails too), not confirmed with a dedicated finding entry |
| household_tour | 3/4 | HARNESS (likely) | same bedroom; bathroom leg proven |
| pantry_count_jars | 1/2 | AGENT | the misfiled-jar trap caught two strong agents — the task working as designed: real signal about carefulness, not an apparatus problem |
| pantry_stocktake | 0/5 | AGENT + HARNESS | same trap at goal 0 (agent); "ordered latching then hides 3 completed subtasks" is a genuine judge-sequencing bug (harness) layered on top -- two separate defects in one row, not a split verdict on one |
| rounds_all_doors | 0/4 | AGENT | agent mistook a door post for the room; narrow_door (a control) proves the judge gives entry credit correctly, so this is a perception/identification miss, not a scoring one |
| rounds_deliver_book | 2/3 | AGENT | book found and picked (post-fix, HARNESS side already resolved); delivered to the wrong landmark -- a navigation/identification error |

Where the live robot fails one of the 36, the failure is downstream of the
system under test. Where it fails one of the nine, the `kind` column says
whether that's benchmark unfairness (HARNESS, always fixed on sight, and
that includes budgets that had not yet been calibrated fair against a
real physical cost) or the actual MARS decision-making falling short
(AGENT) -- the only one the take-home is asking to measure, and the only
verdict a fixed hardware property is ever allowed to resolve to once the
harness has done its job.

---

## The two live runs, side by side

Same robot, same brain, same maps. Between them: every fault in this ledger
found and fixed, 26 of 45 challenge definitions corrected, and a strong-agent
probe certifying 36 of 45 solvable (92 episodes).

| | Aug 16 (pre-audit) | Aug 17 (final benchmark) |
|---|---|---|
| challenges | 4/45 | 5/45 |
| goals | 18/121 | 22/121 |

The Aug 17 column is a composite made honest the hard way: the first pass
silently skipped household, bridge and blaze (a wedged Docker daemon broke
the container's network mid-run; the runner's auth pre-flight refused each
map and the sweep still printed "done"), and rounds' two long errands ran
with their briefs undelivered after the priming scripts timed out. A patch
run the same night re-executed all four bundles fresh -- landing, after
every repair, on the identical headline totals. The lesson is recorded here
because it is the project's oldest one wearing a new coat: the REPORTING
pipeline can also fail quietly, so the final aggregation asserts it holds
exactly 45 same-day episodes before it prints a number.
| observation+conversation | 2/13 | 3/13 |
| simple instruction | 2/17 | 2/17 |
| long-horizon | 0/15 | 0/15 |
| cost | $7.60 | $6.05 |

The near-identical totals are the finding. The audit moved the *achievable*
score from unknown to a certified 36/45 while the robot's score barely moved
— so the 31-challenge gap is now attributable to the system under test, not
the apparatus. New passes are the two conversation probes built this session
(follow_up 2/2 in 14 s, unspoken_request 2/2): the brain converses better
than it does anything else. The stable zeros are exploration (no map, no
semantic navigation — its own words: "I don't have a map ... but I can move
around locally"), manipulation approach, and long-horizon thread-keeping
(0/15 in both runs, with first-goal partials throughout). Run-to-run variance
is real: route_change passed on Aug 16 and not on Aug 17; single-run results
on this system are indicative, not precise.

---

## Robot faults — actual results

### R4. It declines correctly, but from across the room

`counter_out_of_reach` — *"there's a green teapot on the top shelf, can you
bring it to the counter?"* The agent said **"I can't reach the green teapot on
the top shelf"**, which is exactly right, and never moved: path **0.00 m**.

The words are correct and the behaviour is not. The brief asks it to bring
something; it judged reachability from the far side of the room and declined.
That may even be good robotics — do not drive somewhere to discover you cannot
reach — but it is not what it was asked, and the approach goal is what makes
the difference visible instead of scoring it as a clean refusal.

### R5. Six turns is not enough for a fetch, and it does not know that

`counter_within_reach` — the reachable half of the pair. The agent drove
**2.04 m**, looked, drove again, turned twice, and tried to pick: *"nothing
within reach; get closer"*. Then it was out of turns.

It was going the right way and moving deliberately. It simply spent its budget
on approach, and nothing in its behaviour suggests it was tracking how many
turns it had left — the count is in every observation it receives.

---

## Live-stack faults — root causes, and what is still unproven

Found by running Innate's own `brain_client` + Gemini 3.6-flash against the
café map. Category 1 scored **2/4 challenges, 3/7 goals, $0.2494 over 91 calls**
($0.00274/call, 3,596 in / 11 out average). Both passes answered in under three
seconds; both failures ran the full 120 s.

Read the logs from `sim/launcher/.state/ros-log/`, not from `tmux capture-pane`.
The panes hard-wrap at their width, so every long line loses its tail — which is
why the error below read as `RemoteProtocolEr` for hours and the nav goals were
invisible entirely. Four earlier misdiagnoses trace back to reading truncated
output.

### L1. The robot was offered tools that cannot work here — my fault

`navigate_with_vision` fails in about 200 ms with `Missing INNATE_SERVICE_KEY
for UniNavid`, and `search_memory` fails with `Server disconnected without
sending a response`. My `prime_brain.sh` enabled both.

This is not a neutral mistake. Watch the sequence: the model wants to find
something it cannot see, calls `navigate_with_vision`, gets a failure faster
than it can react to, and answers `wait({})`. That is the entire "the robot
gives up" behaviour I had been attributing to the agent.

**The two failures do not have the same cause, and assuming they did was my
next mistake.** UniNavid genuinely needs the service key — the error says so.
`search_memory` does not: with no proxy configured (the log confirms `Proxy not
configured`), `pick_rest` falls through to the direct Google path and the memory
tier talks to `generativelanguage.googleapis.com` with `GEMINI_API_KEY`. Its 13
upload failures and 2 search failures were **every one** a
`RemoteProtocolError` and **not one** a `GeminiHttpError` — no 401, no 403, no
404. Nothing refused a credential. They were dead sockets: the same bug as L2,
in the REST client that `patch_stream_retry.py` did not cover.
`patch_rest_retry.py` covers it now.

**Does the brain need memory? No.** The node says so where it builds its
collaborators: memory is written by a background recorder and read by skills
through `/brain/search_memory`, and "the agent itself knows nothing of it".
`MemorySearch` is already constructed conditionally — withhold the REST client
and the tier does not exist. It is off (`BRAIN_DISABLE_MEMORY=1`) because it
earns nothing here — the brain is reset before every challenge and each one is
self-contained — and because its uploads and cache creation go through the REST
client, which has **no metering tap**, so whatever they spend never appears in
the cost figures above.

Fixed: each skill is now gated on what it actually needs. `search_memory`
follows `BRAIN_DISABLE_MEMORY`, not the key. `navigate_with_vision` stays out
until `BENCH_ALLOW_UNINAVID=1` asks for it. The roster is printed per run.

### L1b. What actually blocks 19 of 38 challenges is grasp vision

`pick_any_object` is the only skill that cannot run here. Its `execute()` fails
on the first line when `self._proxy is None`, because finding the object and
verifying the grasp are vision calls.

**It is not the Innate key it needs, despite what the error says.** The chain is
`pick_any_object._proxy` → `innate.gemini.make_client()`:

```python
client = ProxyClient()                  # available iff INNATE_SERVICE_KEY is set
if client.is_available():               # proxy_url already defaults to Innate's
    return client
base = os.environ.get("GEMINI_BASE_URL", "").strip()
return _DirectClient(base) if base else None
```

`None` — and therefore the failure — needs **both** to be unset. Innate built
that fallback for exactly this case; `_DirectClient`'s own docstring says so:
*"a dev setup with no service key gets a working brain and skills that still
fail with 'Innate proxy not configured'. GEMINI_BASE_URL now covers both."*

And `GEMINI_BASE_URL` is unset here **because I unset it** — the `.env` comment
records why: it used to point at a local Codex shim for model-swap testing, and
I cleared it when wiring the real Gemini key into the brain. Clearing it
silently disarmed the vision seam for skills, which is a different seam from
the brain's transport. So this is my fault, not a missing entitlement, and I
had it in FINDINGS as "needs INNATE_SERVICE_KEY" until it was checked properly.

Two caveats before treating the base URL as a free fix. `_DirectClient` sends
**no auth header** and appends a fixed `/v1/chat/completions`, so it targets an
unauthenticated OpenAI-compatible endpoint. Google's OpenAI-compatible surface
wants `Authorization: Bearer` and lives at `/v1beta/openai/chat/completions` —
both the auth and the path disagree, so `GEMINI_BASE_URL` cannot point straight
at Google. A small local forwarder that injects the key and rewrites the path
bridges it, which is what the old Codex shim was doing. The model is also
hardcoded to `gemini-3.5-flash` in `innate/gemini.py`.

So there are two ways to unblock grasping: an Innate service key with `gemini`
access (the supported path), or a local forwarder behind `GEMINI_BASE_URL`. The
capability gate accepts either. The forwarder is built and in use —
`gemini_shim.py`, with all 38 challenges runnable again.

**The shim has to serve both seams, not one.** `GEMINI_BASE_URL` is read by
`innate/gemini.py` *and* by `brain/transport.py:35`, so anything that only
speaks OpenAI-compatible chat would fix grasping and break every turn. It is
therefore a transparent key-injecting reverse proxy, not a translator:
`/v1/chat/completions` → `/v1beta/openai/chat/completions` with a Bearer
header, everything else passed through untouched with `x-goog-api-key`. The
model is relayed as sent — gemlib asks for `gemini-3.5-flash`, which Google
serves, so skill vision runs on the model it would have used anyway.

**Streaming is the part that nearly shipped broken.** `HTTPResponse.read(n)`
blocks until it has *n* bytes or the stream ends, so an 8 KB chunk size holds a
whole SSE response back — and a turn here averages 11 output tokens, far under
8 KB, so every turn would have arrived in one lump at the end. `read1` returns
what has already landed. Verified end to end: the shim relays its first byte at
1.2 s and finishes at 1.5 s, and its overhead against a direct call to Google is
within noise (−0.25 s to +0.11 s over three runs).

Two of my first three attempts at *testing* that were wrong, which is worth
recording because both failure modes look like a red test:

* asserting an absolute inter-chunk gap measures how Google paces its events,
  not what the shim does;
* comparing time-to-first-chunk against a separate direct call compares two
  independent generations — it reported buffering (4.65 s vs 1.96 s) when the
  shim was fine, purely because that generation was slow.

The check that holds is within-call: a buffering proxy delivers every chunk at
the same instant, so a first-to-last spread near zero is the signature and needs
no control at all.

`live_map.sh` starts the shim and health-checks it **from inside the
container** — `host.docker.internal` is a different hop from `127.0.0.1`, and
checking the easy one would pass while the real one failed. Without the shim,
both the turns and the grasps fail, and the symptom is a robot that sits still:
the same signature as half the faults in this document.

Derived from the goals rather than guessed — a goal that puts a non-robot object
somewhere it does not start can only be satisfied by carrying it:

| category | total | needs a pick |
|---|---|---|
| 1 — observation & conversation | 11 | 1 |
| 2 — simple instruction following | 14 | **12** |
| 3 — long-horizon | 13 | 6 |

Category 2 is 86% unmeasurable without the key. Running those anyway produces
nineteen confident zeros that read as an agent which cannot follow
instructions, when the truth is a capability that was never wired up — so
`live_runner` now reports them **BLOCKED**, with the reason, and excludes them
from the denominator. `--ignore-capabilities` runs them anyway.

The derivation cost me three wrong answers before it was right, which is why it
is a module with a test (`capabilities.py`, `test_capabilities.py`) and not
three lines inside the runner:

* `Hold` is a **duration wrapper**, `Hold(inner=…, seconds=…)` — not a grasp.
  Reading the name literally blocked five category-1 challenges whose goal is
  `Hold(inner=InRect(target='robot', …))`: standing still in a doorway.
* A placement the **setup already satisfies** is a stay-put goal.
  `counter_out_of_reach`'s third goal is `InCircle('counter_teapot', -2.10,
  -0.30, 0.30)` over a Drop at exactly `(-2.10, -0.30)` — it means *leave it
  alone*, and the challenge's correct outcome is that nothing moves.
* Dropping the `target == 'robot'` check for `InCircle`/`InRect` made every
  navigation goal look like a carry. This one survived a clean 101-pair
  equivalence diff against the previous implementation, because that diff can
  only pass `prop=<a dropped object>` and `robot` is never a Drop. It surfaced
  as `rounds_all_doors` — four `InRect(robot)` goals and an empty setup —
  being reported blocked. A test that agrees with an old implementation is not
  the same as a test that agrees with reality.

`lint_reach.py` now imports this rule instead of keeping its second copy; the
two agree on all 101 (challenge, prop) pairs and all 8 maps still lint clean.

### L2. The model connection drops mid-episode — theirs

Five `Turn failed: RemoteProtocolError('Server disconnected without sending a
response.')` in one category-1 run, each on the line directly after `[Brain]
Turn input:`. The brain retries and recovers, but each costs about seven
seconds of a 120 s budget.

`direct_transport` keeps one `httpx.Client` for the process and reuses its TLS
connection across turns. Google's front end closes idle keep-alives on its own
schedule; a connection closed at the far end still looks usable locally, so the
request goes out and the socket is gone before any response byte arrives —
exactly what the error says. Longer gaps between turns make it likelier, and my
own idle-block patch deliberately made the gaps longer.

Fixed in `patch_stream_retry.py`: expire pooled connections after 30 s, and
retry the stream once **only before the first chunk**. The error guarantees the
server processed nothing, so the retry cannot duplicate a reply; after chunks
are out it still fails, because a retry there would replay half an answer.

### L3. Navigation plans through walls it has never looked at — theirs

`distance_remaining` stepped between ~4.3 m and ~8.8 m from one second to the
next, inside a room measuring 4.75 × 3.75 m, with `number_of_recoveries` pinned
at 4 and completion at 0% for 76 consecutive updates. One goal, one action, and
a robot capped at 0.30 m/s — so the path was changing, not the pose.

I checked the pose theory first and it is wrong: `diag_localise.py` scores
73,440 candidate poses against the scan the true pose would produce, and the
truth wins by 3.09×. The map is not ambiguous.

The goals explain it. `go_to_point_in_view` grounds a floor pixel through
`grounding.approach_goal`, which emits `local_frame: true`, and
`navigate_to_position` routes those through the **mapfree** behaviour tree. The
exported AMCL map is not involved at all. The mapfree global costmap is a
10 × 10 m rolling window with no static layer, `track_unknown_space` commented
out (nav2 defaults it to false, so unknown counts as *free*), and:

    raytrace_max_range: 3.0
    obstacle_max_range:  2.5

while the simulated lidar returns to **12.0 m**. Past 2.5 m nothing is ever
marked, so the planner sees open floor in every direction — walls included —
out to a window edge beyond the building. The two goals recorded in the last
run were 3.03 m and 1.98 m away: the first sits squarely in that fiction. The
robot drives, the wall appears inside 2.5 m, the path is invalidated, and the
replan can come back around the other side.

Fixed in `patch_costmap_range.py`: mark to 8 m, clear to 9 m, in all three
costmaps. Still well inside the sensor's 12 m so a no-return is never taken for
an obstacle. This changes no physical property of the robot — the lidar's range
is what it always was; nav2 was discarding data it already had.

### L4. The exported nav map claimed the world outside the building — mine

Found while chasing L3, and real independently of it. `lint_navmap.py` is the
regression test; it failed on both counts and now passes.

* **Unknown read as free.** nav2 reads `occ = (255 - grey)/255`; unknown is grey
  205, so `occ = 0.19608`, and the ROS convention pairs it with `free_thresh:
  0.196` precisely so it lands *above* the line. The exporter wrote `0.25`, so
  every unknown cell came back as free floor and `planner.yaml`'s
  `allow_unknown: false` had nothing left to refuse.
* **The exterior, scanned.** Scan origins were every free cell of the collision
  grid, whose bounds cover the whole model including ground plane past the
  walls. Casting from out there painted the exterior as genuine free floor: on
  the café bundle **78% of the map** (25,089 of 32,214 cells) was open apron the
  robot can never reach, 23,347 of them with better clearance than the furnished
  interior.

**My first fix for the second fault only worked on one world, and I reported it
as general.** Restricting free space to what is 4-connected-reachable from the
spawn removes the apron on a SEALED room — the café — and does nothing on any
world with a doorway, because a flood fill has no notion of a building
envelope: it walks out through the 0.6 m gap and keeps the whole exterior
ground plane. Measured after that "fix":

| world | apron | cells dropped |
|---|---|---|
| counter (café, sealed) | 0% | 81% |
| pantry | **79%** | 0.2% |
| blaze | **81%** | 1.4% |

Now clipped to the building envelope as well, and all 8 worlds measure 0%
apron. Note the exterior is genuinely traversable in the physics world — there
is a ground plane out there — so this is not a correction to the scan. It is a
decision that the nav map covers the building, so the planner cannot answer
"go to the far corner" with a stroll around the outside.

Two claims I made in this file were also false and are corrected: the origins
were never restricted (`core.py` still scans from every free cell of the
collision grid; the restriction is entirely post-hoc), and the fault is not
hidden by bounds that hug the walls — every bundle's bounds run 3–9 m past
them. What hid it was the café's sealed geometry.

**And the lint that was supposed to catch it could not.** Its second check
compared routes over `true_free = (occ < 0.196) & (grey != 205)` against
`read_free = occ < free_thresh`. But `occ(205) = 0.19608` is not below 0.196,
so the `grey != 205` term excludes nothing the first term kept: once
free_thresh is correct the two masks are **bit-identical**, the gap is
identically zero, and it reported "0 cheated cells" by construction rather than
by evidence. It could only ever fire in the case the *first* check already
catches. It now measures the apron directly — drivable floor connected to the
map border — and is validated against a synthetic leaky map before being
trusted on a real one.

### L5. "Assume perfect map" is an assumption this agent cannot use

Innate's guidance was *"let's assume perfect map at first"*. The stack can be
given one — `export_nav_map.py` builds it from the loaded world's own geometry
with a virtual lidar at the true laser height, and AMCL localises against it.
The agent's main visual-navigation primitive still cannot use it.

`grounding.approach_goal` **hardcodes `local_frame: True`** (grounding.py:129),
and `navigate_to_position` sends any local-frame goal through the mapfree
behaviour tree and the mapfree costmap — a 10 × 10 m rolling window with no
static layer. So every `go_to_point_in_view` plans against the last few seconds
of `/scan`, however good the map is and however well localised the robot is.
The stack was in map mode throughout the runs above; this is per-goal, not a
mode setting.

**This is a defensible default, not a bug.** A local-frame goal is immune to
localisation error: if AMCL is 2 m out, a map-frame goal goes 2 m wrong while a
local one still lands where the camera pointed. On a real robot in an unmapped
house that robustness beats the map. Under "assume perfect map" the trade runs
the other way — localisation is trustworthy, and the map holds geometry the
current scan cannot see.

So the instruction changes nothing about the challenges (see below) and
everything about which arm is the interesting one.
`BENCH_MAP_FRAME_GOALS=1` (`patch_map_frame_goals.py`) converts the grounded
goal into the map frame after it is re-based onto the robot's current pose, so
the map-frame planner handles it. **Default off**: shipped behaviour gets
measured first, and this is the second arm of an A/B, not a silent change to
someone else's agent.

The transform is the exact inverse of the shipped
`absolute_to_local_nav_command`, verified by round trip over 2,000 random
poses and goals plus fixed cases pinning rotation direction and handedness
(`test_map_frame.py`). A sign error here does not crash — it sends the robot to
a point mirrored about its own heading, which reads as an agent with poor
spatial reasoning. With the stack down, checking it against the transform the
mapfree path already trusts was the only way to know.

### Does a perfect map change the challenges? No.

Checked all 38 briefs against what a perfect occupancy map actually provides.
It gives geometry and localisation. It does not give object identity or
position, semantic room labels, colours, heights, or counts — which is what
every challenge asks for:

* **Category 1** is counting, colour, height and occlusion. `rounds_count_doors`
  is the only one whose answer is geometric, and the map never reaches the
  model: the turn prompt carries the camera and a pose line, nothing else. The
  planner consumes the map; the agent never sees it.
* **Category 2** is fetch-and-place plus colour-named doors and gates.
* **Category 3** is sequencing, tours and deliveries. `household_tour` needs
  rooms identified as living room / kitchen / bathroom / bedroom, which an
  occupancy grid does not label. `rounds_find_bathroom` needs a sink and a
  toilet recognised.

Nothing is trivialised and no challenge is premised on the robot *not* knowing
the layout, so none needs rewriting. What changes is attribution: with a
perfect map assumed, a navigation failure can no longer be excused as "it did
not know the room", which is exactly the excuse L3 and L5 remove.

What the assumption *does* demand is that the harness earn it. `live_map.sh`
now regenerates the map per bundle and refuses to run if `lint_navmap.py`
fails, because both map faults in L4 shipped silently and a run against either
is not a run against a perfect map.

### What is still unproven

I have **not** directly observed the planner choosing a route through an
unobserved wall — the log lines that would show the planned path were truncated
before I found the real log directory, and the stack has been down since. L3's
mechanism is established from the configuration and the goal geometry, not from
a recorded path. The next run should confirm it or contradict it, and until it
does, treat "fixed" as "cause identified and removed", not "measured better".

The category-1 numbers above were produced **before** any of these fixes, with
two dead tools in the roster. They are a floor, not a verdict on the agent. One
of the four, `counter_within_reach`, needs a pick and would now be reported
BLOCKED rather than scored 0/2 — so the honest reading of that run is **2 of 3
attempted**, not 2 of 4.

Nothing here needed the Innate key to find, and — once the chain was actually
read rather than inferred from an error message — nothing needs it to fix
either. 19 of 38 challenges are blocked on grasp vision, 12 of 14 in category
2, so the suite cannot answer its second question until that backend exists;
but either an Innate service key or a local forwarder behind `GEMINI_BASE_URL`
supplies it.

---

## Known limits of this benchmark

Stated here rather than discovered later.

1. **No ASR.** Narrator lines arrive as text. `bridge_stutter` measures what the
   language model does with disfluent *text*; the real stack's speech front end
   is upstream of everything here and is not exercised by any challenge.

2. **The blind control is not a vision result.** `CodexBackend` is text-only
   because the Codex CLI takes no images. Its scores answer "what is reachable
   from the brief alone" and must not be quoted as perception numbers.

3. **"1 in 8" / "1 in 32" on the Bridge is a guessing floor, not environmental
   randomness -- corrected after actually checking the challenge source.**
   `bridge_three`/`bridge_five`'s left/right route (`ROUTE = (...)` in each
   challenge file) is a FIXED sequence, identical every episode, stated
   outright in the brief ("go right, then left, then right"). There is no
   dice roll in the world. "1 in 8" (2^-3) and "1 in 32" (2^-5) describe what
   a non-comprehending agent blind-guessing at each independent binary gate
   would achieve by luck -- a floor under a meaningless PASS, not a ceiling
   a working agent should expect to bump into. A single PASS is still weak
   evidence on its own (it could be that lucky guesser). A single FAIL, and
   especially a REPEATED, IDENTICALLY-REASONED fail, is not weak evidence at
   all -- it is exactly the AGENT-capability signal this benchmark exists to
   surface (see the repeat-run results in NEMOTRON_STACK_RESULTS.md, which
   found consistent, repeatable failures on both, not scattered chance
   losses).

4. **RHAE has no human baseline.** ARC-AGI-3's efficiency score is
   `min(1, h/a)²` where `h` is the second-best *human's* action count. No human
   data has been collected, so the derived plan's step count stands in. It is a
   score against a reference plan, not against a person, and is labelled as such.

5. **`fail_if` is evaluated at 10 Hz.** A robot moving faster than ~2 m/s could
   in principle cross an 18 cm elimination band between ticks. Nothing in this
   sim moves that fast (V_MAX is 0.30 m/s), but the band width is a function of
   speed and would need revisiting if it changed.

6. **The oracle proves solvability, not sanity.** It is deaf by construction and
   plans straight to the final state. It cannot tell you a challenge is
   confusing, ambiguous, or badly worded — only that some agent could satisfy
   the goals. Every challenge here still needs a human to read it.

7. **Rooms have no ceiling geom, and their walls are separate boxes, not a
   sealed shell.** At most camera angles this is invisible (the background
   above wall-height is a flat black "sky," same as any open-air view). At a
   narrow band of oblique angles near a wall corner, the seam between two
   non-abutting wall pieces can let that black background show through as a
   small rectangular patch — confirmed harmless (a second-pass fairness probe
   flagged one, on `blaze_l4`; it was absent from the immediately preceding
   turn's frame, i.e. present only from that one grazing angle) but real, and
   worth knowing before reading a black patch in a frame as a missing object
   or an occlusion. A true fix means giving every room a real sealed shell,
   which — like the rooms themselves — is generator output ("do not
   hand-edit: rebuild the map and re-run the exporter"), so it is disclosed
   here rather than patched by hand.
