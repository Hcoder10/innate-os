#!/usr/bin/env python3
"""Print the measured columns of the last sweep, one row per episode.

Separate from main.py's summary on purpose: that summary answers "is this
suite valid, and did the agent pass", and this answers "how did it get there".
Kept apart so the pass/fail table stays readable at ninety challenges.

  path_m    distance actually driven, integrated per tick. Not start-to-end
            displacement: an agent that visits three wrong rooms and comes
            back has the same displacement as one that never moved.
  turns     agent decisions. For the oracle this is the derived plan's step
            count, which is the reference an agent's number is read against.
  1st_utt   seconds to the robot's first word. A robot that says nothing for
            four minutes and then succeeds is a different product from one
            that acknowledges the request immediately.
  heard     narrator lines delivered to the agent this episode.
  tempt_m   closest the robot ever got to the object an ambient cue pointed
            at, AFTER that cue landed. Blank when the challenge has no
            temptation. Small numbers mean it went to do what it overheard.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def fmt(v, spec: str = "{:.2f}") -> str:
    return "-" if v is None else spec.format(v)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/bench_results.json")
    only = sys.argv[2] if len(sys.argv) > 2 else None

    rows = json.loads(path.read_text())
    if only:
        rows = [r for r in rows if r.get("agent") == only]
    rows.sort(key=lambda r: (r.get("map", ""), r.get("challenge", ""), r.get("agent", "")))

    hdr = (
        f"{'map':<10} {'challenge':<30} {'agent':<8} {'goals':>7} "
        f"{'path_m':>7} {'turns':>6} {'utt':>4} {'1st_utt':>8} {'heard':>6} {'tempt_m':>8}  goal_times_s"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        goals = f"{r.get('goals_done', 0)}/{r.get('goals_total', 0)}"
        print(
            f"{r.get('map', ''):<10} {r.get('challenge', ''):<30} {r.get('agent', ''):<8} "
            f"{goals:>7} {r.get('path_len_m', 0.0):>7.2f} {r.get('turns', 0):>6} "
            f"{r.get('utterances', 0):>4} {fmt(r.get('first_utterance_s'), '{:.1f}'):>8} "
            f"{r.get('heard', 0):>6} {fmt(r.get('tempt_min_m')):>8}  {r.get('goal_times_s', [])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
