# Applied changes

Each entry below was an idempotent patch script that rewrote a repo file in
place. All of them have been applied; the scripts themselves are deleted and
this file is their record. The full reasoning for the load-bearing ones lives
in FINDINGS.md.

## patch_after

Give predicates a clock, so a world can change while the robot is in it.

## patch_agent_speech

Give the scripted oracle a voice and a turn count.

## patch_answer

Make an answer scoreable however the robot chooses to give it.

## patch_audit

Two bugs an end-to-end audit found that the unit tests did not.

## patch_blind_menu

Expose `thinking`, and give a blind backend a menu without a camera in it.

## patch_brain_wiring

Register BrainAgent with the sweep, so --agents brain:<backend> works.

## patch_category

Give every challenge a category, so the suite can score the three things.

## patch_chat_bridge

Let the engine hear the robot SPEAK, not just act.

## patch_control

Finish the three eye/answer fixes properly. Each was applied but incomplete.

## patch_costmap_range

Let the costmaps use the lidar they already have.

## patch_extract

Replace the regex JSON extractor with a real scanner.

## patch_eyes

Give the agent working eyes: right camera, right resolution, frames on disk.

## patch_failif

Give a Challenge a way to END BADLY, not merely to run out of clock.

## patch_goal_height

Let a placement goal require the object to be ON the surface, not beside it.

## patch_idle_block

Stop the brain paying Gemini to tell it to keep waiting.

## patch_lintfork

The lint was lying about every map except the first one in its argument list.

## patch_map_frame_goals

Let "go to that thing I can see" use the map, when there is a good one.

## patch_memory_off

Let a deployment with no memory backend stop pretending it has one.

## patch_meter_dedupe

Meter each model call ONCE, not once per chunk that mentions usage.

## patch_narrator

Add the narrator, Said, and engine-side metrics to challenges.py.

## patch_observe

Let a predicate watch the whole run, not only its own turn.

## patch_outdir

Stop writing results to /tmp.

## patch_proxy_meter

Meter the PROXY path too, or switching backends blinds the benchmark.

## patch_rest_retry

The other half of the dropped-connection fix.

## patch_scorecard

Report a score per CATEGORY, which is the thing that was actually asked for.

## patch_slab

Make the nav map tell the truth about what blocks a base that cannot climb.

## patch_stall

Report a stalled leg as a stall, instead of spending the clock and blaming it.

## patch_stream_retry

Stop a dead pooled connection from costing the robot a turn.

## patch_tempt

Replace the ambient-compliance flag with a targeted one.

## patch_thinktime

Stop model latency from burning sim time at 10x real speed.

## patch_usage_meter

Record what every Gemini call actually costs, from inside the brain.

## patch_vision_meter

Meter the SKILL-VISION calls, so the reported cost stops being a floor.
