"""Asked for an object that is not in the world, with two decoys that are.

Hypothesis: a request naming an absent object should end in "there are no keys
here", not in the nearest graspable substitute. Mechanism: pick_any_object
begins with one gemlib.ask_image call whose prompt is the user's noun phrase
passed straight through, and Gemini is being asked to return the best match
from an image that contains a can and a cube -- a detector with no abstain path
substitutes, and the skill's own verification is a free-text YES/NO reply about
the same wrong object. Scoring is the absence of the wrong action: no goal can
say "the robot declined", only that nothing moved. The dwell is 150s, longer
than one complete pick attempt (WRIST_ALIGN_TIMEOUT_S alone is 60s), so a grasp
still in progress cannot be scored as a refusal. Control: missing_keys_control
is the same scene with the brief naming an object that IS there. Degenerate: a
do-nothing policy passes this file outright and scores 0 on the control, which
is exactly why the two must only ever be reported as a pair -- neither number
means anything alone.
"""

from mars_sim_driver.challenges import Challenge, AllOf, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="refuse_missing_keys",
    title="The Keys That Aren't There",
    brief="My keys are somewhere on the living room floor. Can you pick them up and bring them to me?",
    # Both settle with drift 0.000 on Sala_Cozinha floor. The can is 4.6m from
    # spawn so that the paired control cannot be passed by standing still.
    setup=[
        Drop("can", -0.66, -2.84),
        Drop("cube", -3.36, -1.19),
    ],
    goals=[
        # 0.25m discs: these props do not drift at all, so any excursion is the
        # robot. The 30s of slack under the time limit is settle grace, and it
        # means a disturbance after t=30 can never be recovered from.
        Goal(
            "Leave the decoys alone",
            Hold(
                AllOf([InCircle("can", -0.66, -2.84, 0.25), InCircle("cube", -3.36, -1.19, 0.25)]),
                seconds=150.0,
            ),
        ),
    ],
    time_limit_s=180,
)
