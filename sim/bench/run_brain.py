#!/usr/bin/env python3
"""Run the LLM agent over one map's challenges, sequentially, with a transcript.

Sequential and not parallel, deliberately: one model call per turn means the
episodes are latency-bound rather than CPU-bound, and running eight at once
just multiplies the rate-limit surface. It also keeps the printed transcript
readable, which is the point -- a pass/fail row says the agent failed, and the
turn-by-turn log says what it did instead.

  ./sim/.venv/bin/python sim/bench/run_brain.py counter --backend codex
  ./sim/.venv/bin/python sim/bench/run_brain.py counter --only counter_read_the_pass
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parents[1]
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))
os.environ.setdefault("MUJOCO_GL", "osmesa")

from backends import BACKENDS  # noqa: E402
from brain_agent import BrainAgent  # noqa: E402
from runner import run_episode, sources  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("map")
    ap.add_argument("--backend", default="codex", choices=sorted(BACKENDS))
    ap.add_argument("--only", default="", help="one challenge id")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--out", type=Path, default=BENCH / "results" / "brain_results.json")
    args = ap.parse_args()

    from mars_sim_driver.challenges import load_challenges

    assets, ch_root = sources()[args.map]
    ids = sorted(load_challenges([ch_root]))
    if args.only:
        ids = [c for c in ids if c == args.only]
        if not ids:
            print(f"no challenge {args.only!r} in {args.map}")
            return 1

    rows = []
    for cid in ids:
        held: dict = {}

        def make(ch, _held=held):
            agent = BrainAgent(BACKENDS[args.backend](), max_turns=args.max_turns)
            agent.name = f"brain:{args.backend}"
            _held["agent"] = agent
            return agent

        wall0 = time.time()
        # Camera-native 640x480. The default 160x120 is a speed optimisation
        # for agents that never look, and it costs a vision agent the task:
        # same scene and model, 640x480 reads three cups correctly and 160x120
        # reads two, because the third genuinely is not there at that size.
        ep = run_episode(args.map, cid, make, render_wh=(640, 480))
        agent = held.get("agent")
        log = agent.transcript() if agent else []

        mark = "PASS" if ep.passed else "fail"
        print(f"\n{'=' * 78}\n{mark}  {cid}  {ep.goals_done}/{ep.goals_total}  "
              f"turns={ep.turns}  path={ep.path_len_m}m  sim={ep.elapsed_s}s  "
              f"wall={round(time.time() - wall0)}s  {ep.error or ep.reason}")
        for e in log:
            print(f"  +{e['t']:6.1f}s [{e['latency_s']:5.1f}s] {e['action']:<8} "
                  f"{str(e['args'])[:44]:<46} -> {e['result']}")

        row = ep.__dict__.copy()
        row["turn_log"] = log
        rows.append(row)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        # Written after EVERY episode: a sweep that is killed part-way still
        # leaves the episodes it finished, which is the only reason a long
        # latency-bound run is worth starting at all.
        args.out.write_text(json.dumps(rows, indent=1, default=str))

    passed = sum(1 for r in rows if r["passed"])
    goals = sum(r["goals_done"] for r in rows), sum(r["goals_total"] for r in rows)
    print(f"\n{'=' * 78}\n{args.map} / brain:{args.backend}: "
          f"{passed}/{len(rows)} challenges, {goals[0]}/{goals[1]} goals -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
