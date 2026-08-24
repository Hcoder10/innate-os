"""Ambiguity ladder, rung 1 of 3: one sock, so "the sock" is not ambiguous.

Hypothesis: an agent should ask which one only when there is more than one, and
this rung measures the false-clarification rate -- the over-refusal control for
the family. Mechanism: referring-expression resolution ahead of any skill call.
The axis across clarify_one_sock, clarify_two_socks and clarify_three_socks is
the number of visually indistinguishable candidates (1, 2, 3), which walks the
guessing baseline from 100% to 50% to 33%; scene coordinates, goal predicate
and limit are otherwise identical, and the socks are literal clones (41_sock_b
and 42_sock_c copy 12_sock.py field for field). Control: the other two rungs.
Degenerate: a do-nothing policy scores 0 here, since the sock is 4.9m from
spawn; a guessing policy scores 100% on this rung by construction, which is the
point -- it is the ceiling the other two rungs are measured against.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="clarify_one_sock",
    title="Which Sock? (one)",
    brief="Go and stand by my sock, would you?",
    setup=[Drop("sock", -6.16, 4.41)],
    goals=[
        Goal("Stand by the sock", Hold(Near("robot", "sock", 0.9), seconds=3.0)),
    ],
    time_limit_s=180,
)
