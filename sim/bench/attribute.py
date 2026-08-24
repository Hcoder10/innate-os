#!/usr/bin/env python3
"""Classify every failed episode by WHICH LAYER failed.

WHY THIS MATTERS MORE THAN THE SCORES. A pass rate says a system is at 40%. It
does not say whether the other 60% is a brain that chose the wrong object or a
base that could not drive to the right one -- and those have different owners
and different fixes. That distinction is worth most exactly when the navigation
stack is being replaced, because it is the only way to tell whether the new one
changed anything: re-run the identical suite and watch the LOCOMOTION column,
not the total.

Nothing here needs new instrumentation. Every signal is already recorded per
episode by the runner, and the classification is a set of stated rules over
them rather than a judgement:

  HARNESS      the measuring apparatus failed -- a backend error, an engine
               error, a missing prop. Not a result. Excluded from any rate.
  LOCOMOTION   the robot could not get where it had decided to go: a named
               stall, or a path far longer than the reference while goal
               progress sat still.
  SEARCH       it kept moving and never found the thing. Long path, no stall,
               out of time. Perception or exploration, not the base.
  DECISION     it moved decisively to somewhere and was wrong -- eliminated by
               a fail_if, or it stopped early with goals open.
  SILENT       everything physical was fine and an utterance goal never fired.
               It was in the right place and did not say the right thing.
  ANSWERED-WRONG  it spoke and then stopped. It committed to an answer and the
               answer was wrong -- which is the opposite finding from giving up.
  GAVE-UP      it stopped of its own accord, said nothing, and barely moved.
  UNKNOWN      does not match any rule. Printed with its signals so the rule
               set can be extended rather than the episode quietly binned.

The rules are deliberately crude and deliberately visible. A misclassification
should be arguable from the printed row, which is why every row carries the
numbers it was judged on.

  ./sim/.venv/bin/python sim/bench/attribute.py            # every results file
  ./sim/.venv/bin/python sim/bench/attribute.py brain:codex
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# A path this many times the reference plan's, or more, is wandering.
LONG_PATH = 1.6
# Below this fraction of the reference path, the robot effectively did not go.
BARELY_MOVED = 0.25


def classify(ep: dict, ref: dict | None) -> tuple[str, str]:
    """(layer, why). `ref` is the oracle's episode on the same challenge."""
    reason = (ep.get("reason") or "").lower()
    error = (ep.get("error") or "").lower()

    if ep.get("camera_errors", 0):
        # It could not see. Whatever else went wrong, this is not a result
        # about perception or about decision-making.
        return "HARNESS", f"camera failed {ep['camera_errors']}x"
    if error or "challenge error" in reason or "backend error" in reason:
        return "HARNESS", (error or reason)[:60]
    if "engine.start" in reason or "no prop named" in reason:
        return "HARNESS", reason[:60]

    if "stuck at" in reason:
        return "LOCOMOTION", "stall reported by the follower"

    path_ratio = None
    if ref and ref.get("path_len_m", 0) > 0:
        path_ratio = ep.get("path_len_m", 0.0) / ref["path_len_m"]

    # Eliminated by a fail_if: it committed to a route and the route was fatal.
    # That is a decision, not a driving failure -- it got where it was going.
    if reason and reason not in ("time limit", "") and "agent finished" not in reason:
        return "DECISION", f"eliminated: {reason[:40]}"

    goals_done = ep.get("goals_done", 0)
    goals_total = ep.get("goals_total", 0)

    if "agent finished" in reason or ep.get("turns", 0) and goals_done < goals_total and "time limit" not in reason:
        # It SPOKE and then stopped: it committed to an answer. That is a wrong
        # answer, not a refusal to engage, and the two are opposite findings.
        # The first version filed a confidently wrong count under GAVE-UP
        # because the robot had not moved.
        if ep.get("utterances", 0) > 0:
            return "ANSWERED-WRONG", (
                f"said something and stopped at {goals_done}/{goals_total}"
                + (f", path {path_ratio:.2f}x" if path_ratio is not None else "")
            )
        if path_ratio is not None and path_ratio < BARELY_MOVED:
            return "GAVE-UP", f"stopped with {goals_done}/{goals_total}, path {path_ratio:.2f}x, said nothing"
        return "DECISION", f"stopped with {goals_done}/{goals_total} open"

    if "time limit" in reason:
        if path_ratio is not None and path_ratio >= LONG_PATH:
            return "SEARCH", f"path {path_ratio:.2f}x the reference, never arrived"
        if path_ratio is not None and path_ratio < BARELY_MOVED:
            # It stood still for the whole clock. If it also said things, the
            # physical side was never the problem.
            if ep.get("utterances", 0) > 0:
                return "SILENT", f"talked, path {path_ratio:.2f}x -- never acted"
            return "GAVE-UP", f"path {path_ratio:.2f}x -- barely moved"
        return "SEARCH", "out of time while moving"

    return "UNKNOWN", f"reason={reason!r} goals={goals_done}/{goals_total}"


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    rows = []
    for f in sorted(RESULTS.glob("bench_*.json")) + sorted(RESULTS.glob("brain_*.json")):
        try:
            rows.extend(json.loads(f.read_text()))
        except Exception as exc:  # noqa: BLE001
            print(f"skipping {f.name}: {exc}")
    if not rows:
        print(f"no results in {RESULTS}")
        return 1

    ref = {e["challenge"]: e for e in rows if e.get("agent") == "oracle" and e.get("passed")}
    agents = sorted({r.get("agent", "?") for r in rows} - {"oracle"})
    if only:
        agents = [a for a in agents if a == only] or [only]

    for agent in agents:
        eps = [e for e in rows if e.get("agent") == agent]
        failed = [e for e in eps if not e.get("passed")]
        if not eps:
            continue
        print(f"\n=== {agent}: {len(eps) - len(failed)}/{len(eps)} passed, "
              f"{len(failed)} failed ===")
        if not failed:
            continue

        buckets: dict[str, list] = {}
        for e in failed:
            layer, why = classify(e, ref.get(e["challenge"]))
            buckets.setdefault(layer, []).append((e, why))

        for layer in ("HARNESS", "LOCOMOTION", "SEARCH", "DECISION", "ANSWERED-WRONG",
                      "SILENT", "GAVE-UP", "UNKNOWN"):
            items = buckets.get(layer)
            if not items:
                continue
            print(f"\n  {layer}  ({len(items)})")
            for e, why in sorted(items, key=lambda t: t[0]["challenge"]):
                pr = ""
                r = ref.get(e["challenge"])
                if r and r.get("path_len_m", 0) > 0:
                    pr = f"  path {e.get('path_len_m', 0) / r['path_len_m']:.2f}x"
                print(f"    {e['challenge']:<28} {e.get('goals_done', 0)}/{e.get('goals_total', 0)}"
                      f"{pr}  {why}")

        real = len(failed) - len(buckets.get("HARNESS", []))
        print(f"\n  {len(buckets.get('HARNESS', []))} of {len(failed)} failures were the harness's "
              f"fault and are excluded; {real} are results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
