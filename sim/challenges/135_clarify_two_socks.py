"""Ambiguity ladder, rung 2 of 3: two identical socks, two rooms apart.

Hypothesis: faced with two objects a camera cannot tell apart, an agent should
ask which one rather than commit. Mechanism: referring-expression resolution
ahead of pick_any_object, whose prompt is a bare noun phrase with no slot for
"which"; see clarify_one_sock for the family's axis. Only the final dwell is
scored, so touring both candidates does not beat guessing -- the robot has to
end up at the one the user named. Control: clarify_one_sock (the
false-clarification rate) and clarify_three_socks (the next rung). Degenerate:
a guessing policy scores 50%, so this rung needs repeats and is only meaningful
as a drop from rung 1; a nearest-first policy scores 0, because the bedroom
sock is 4.81m from spawn against the bathroom sock's 4.93m and is the wrong
answer; do-nothing scores 0.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="clarify_two_socks",
    title="Which Sock? (two)",
    brief=(
        'Tell the robot "go and stand by my sock" and nothing else. If it asks '
        'you which one, answer "the one in the bathroom" — but only if it asks.'
    ),
    # sock is the bathroom one and the right answer; sock_b is its clone in the
    # bedroom. Both settle with drift 0.000.
    setup=[
        Drop("sock", -6.16, 4.41),
        Drop("sock_b", -0.56, 2.81),
    ],
    goals=[
        Goal("Stand by the bathroom sock", Hold(Near("robot", "sock", 0.9), seconds=3.0)),
    ],
    time_limit_s=180,
)
