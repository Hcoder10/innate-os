#!/usr/bin/env python3
"""Exercise every part of BrainAgent with a scripted backend and no network.

This exists because the expensive way to discover that the agent loop is broken
is halfway through a paid sweep. EchoBackend makes the turn loop, the motion
primitives, pick/place and the speech channel all runnable offline, so a
regression in any of them shows up in fifteen seconds.

Run:  ./sim/.venv/bin/python sim/bench/test_brain_loop.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parents[1]
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))
os.environ.setdefault("MUJOCO_GL", "osmesa")

from backends import EchoBackend  # noqa: E402
from brain_agent import BrainAgent  # noqa: E402
from runner import run_episode  # noqa: E402

SCRIPT = [
    {"action": "say", "args": '{"text": "On my way."}'},
    {"action": "forward", "args": '{"metres": 1.2}'},
    {"action": "turn", "args": '{"degrees": 30}'},
    {"action": "forward", "args": '{"metres": 0.4}'},
    {"action": "pick", "args": "{}"},
    {"action": "place", "args": "{}"},
    {"action": "say", "args": '{"text": "I cannot reach the top shelf."}'},
    {"action": "finish", "args": "{}"},
]

CHECKS = [
    ("turns were counted", lambda ep, ag: ag.turns == len(SCRIPT)),
    ("the robot actually moved", lambda ep, ag: ep.path_len_m > 0.5),
    ("speech reached the engine", lambda ep, ag: ep.utterances >= 2),
    ("time-to-first-word recorded", lambda ep, ag: ep.first_utterance_s is not None),
    ("every turn is logged", lambda ep, ag: len(ag.transcript()) == len(SCRIPT)),
    ("args parsed from JSON strings", lambda ep, ag: ag.transcript()[1]["args"] == {"metres": 1.2}),
    ("finish ends the episode", lambda ep, ag: ep.reason and "plan" in ep.reason or ag.done),
]


def main() -> int:
    held: dict = {}

    def make(ch):
        agent = BrainAgent(EchoBackend(SCRIPT), max_turns=20)
        agent.name = "brain:echo"
        held["agent"] = agent
        return agent

    ep = run_episode("counter", "counter_out_of_reach", make, max_sim_s=200.0)
    agent = held["agent"]

    print(f"\nepisode: {ep.goals_done}/{ep.goals_total} goals  path={ep.path_len_m} m  "
          f"turns={ep.turns}  utterances={ep.utterances}  reason={ep.reason!r}")
    for entry in agent.transcript():
        print(f"  +{entry['t']:5.1f}s  {entry['action']:<8} {str(entry['args'])[:40]:<42} -> {entry['result']}")

    bad = [name for name, check in CHECKS if not check(ep, agent)]
    print()
    for name, check in CHECKS:
        print(f"  {'ok  ' if check(ep, agent) else 'FAIL'}  {name}")
    if bad:
        print(f"\n{len(bad)} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
