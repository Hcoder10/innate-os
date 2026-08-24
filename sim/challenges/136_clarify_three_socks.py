"""Ambiguity ladder, rung 3 of 3: three identical socks, one per room.

See clarify_one_sock for the hypothesis, the mechanism and the axis; this rung
adds a third clone and drops the guessing baseline to 33%. Control:
clarify_one_sock and clarify_two_socks. Degenerate: guessing scores 33%,
nearest-first scores 0 -- the kitchen sock is nearest to spawn at 3.93m and is
the wrong answer -- and do-nothing scores 0. Three rungs at 100/50/33 is what
makes an asking agent separable from a lucky one: a flat pass rate across the
ladder is guessing, and a rate that tracks the baseline is chance.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="clarify_three_socks",
    title="Which Sock? (three)",
    brief=(
        'Tell the robot "go and stand by my sock" and nothing else. If it asks '
        'you which one, answer "the one in the bathroom" — but only if it asks.'
    ),
    setup=[
        Drop("sock", -6.16, 4.41),
        Drop("sock_b", -0.56, 2.81),
        Drop("sock_c", -5.86, -3.79),
    ],
    goals=[
        Goal("Stand by the bathroom sock", Hold(Near("robot", "sock", 0.9), seconds=3.0)),
    ],
    time_limit_s=180,
)
