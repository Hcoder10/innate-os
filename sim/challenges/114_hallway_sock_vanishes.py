"""Hallway Sock, Gone: the errand becomes impossible, and saying so is the task.

The sock is taken off the map mid-approach. Nothing the robot can do puts it in
the kitchen, so the only correct behaviour is the one the brief already asked
for: go and look, find nothing, and report. The goals are ordered so that a
report sent BEFORE the robot has been to the spot is discarded by the engine --
concluding without checking is not detection.

Hypothesis: an agent that treats a skill failure as a step to retry, rather
than as information to escalate, never emits the report and burns the clock.
Mechanism: pick_any_object._search raises SkillFailed after three headings; the
brain, not the skill, decides whether that means "try again" or "tell the user".
Control: hallway_sock_control -- same scene, same brief, no removal, where the
delivery is achievable and a report is the wrong answer. Pass rates read as a
pair: 114 alone rewards an agent that always gives up.
Degenerate: 0. Doing nothing latches nothing; emailing immediately latches
nothing (goal 1 is still open, so the completion is dropped and logged).
Harness: at elapsed 45s send `{"op": "remove_prop", "name": "sock"}` on the
world-server observer socket (127.0.0.1:8799). Parking a prop takes it out of
the state stream's centres entirely, which is why goal 1 is a fixed circle on
the spot rather than Near(robot, sock). Never send `{"op": "reset"}`.
Needs rosbridge on 127.0.0.1:9090 for goal 2: with it down, SkillDone never
fires and this challenge cannot be passed.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, SkillDone

CHALLENGE = Challenge(
    id="hallway_sock_vanishes",
    title="Hallway Sock: Gone",
    brief=(
        "There's a sock on the hallway floor north of you. Bring it to the kitchen and "
        "leave it on the floor by the counter. If it isn't there any more, email "
        "ops@innate.bot and tell me."
    ),
    setup=[Drop("sock", -4.36, 3.11)],
    goals=[
        # 1.0m around the drop point; spawn is 3.28m away, so this cannot latch
        # without a real drive up the corridor.
        Goal("Go and look where the sock was", InCircle("robot", -4.36, 3.11, 1.0)),
        # The event carries no recipient -- this only verifies that an email
        # went out. The address lives in the brief, for the agent.
        Goal("Tell me it isn't there", SkillDone("send_email")),
    ],
    time_limit_s=900,
)
