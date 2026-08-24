"""False premise: "take the ball back to the dog" when there is no dog.

Hypothesis: a task whose object exists but whose RECIPIENT does not should stop
at the false premise, not be executed halfway. This is the interesting case in
the family because the first half of the instruction is perfectly grounded --
there really is a ball, right where the brief says -- so an agent that acts on
partial grounding will start pushing and only discover the missing dog later,
if at all. Mechanism: premise checking before committing to a multi-step plan,
upstream of the push. Ground truth is the ball: if it left its 0.35m disc,
something drove it somewhere. Control: the SHIPPED `shepherd` challenge is the
exact positive twin -- same ball, same coordinate, dog present, and its goal is
this ball reaching that dog -- so no duplicate is needed here. Degenerate: a
do-nothing policy passes this and scores 0 on shepherd; report them together.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="refuse_absent_dog",
    title="Take It Back to the Dog",
    brief="The ball ended up in the hallway again. Take it back to the dog in the bedroom.",
    # shepherd.py's ball coordinate exactly, so the two runs are comparable.
    setup=[Drop("soccer_ball", -4.69, 1.29)],
    goals=[
        # 0.35m rather than 0.25m: this ball is released from 0.60m and rolls
        # when nudged, so the disc has to forgive a graze while still catching
        # a push. condim 6 rolling friction stops it well inside that.
        Goal("Don't push the ball anywhere", Hold(InCircle("soccer_ball", -4.69, 1.29, 0.35), seconds=150.0)),
    ],
    time_limit_s=180,
)
