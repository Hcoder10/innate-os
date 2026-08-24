#!/usr/bin/env python3
"""Merge per-map result files into one report.

WHY THIS EXISTS RATHER THAN JUST RUNNING EVERY MAP AT ONCE. A whole-suite sweep
takes long enough that it keeps getting reaped by the environment it runs in --
WSL tears down background processes when the session that started them ends, and
a 42-episode run does not fit in one foreground call. Per-map runs each finish
in under two minutes and always complete.

That is a property of THIS machine, not of the benchmark, and the fix is not to
make the sweep shorter. It is to make the sweep resumable, which it now is:
every map writes its own results file, and this stitches them together.

  bash ~/run_bench.sh --map counter --out sim/bench/results/bench_counter.json
  ...
  ./sim/.venv/bin/python sim/bench/report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

CATEGORY_NAMES = {
    1: "easy observation and conversation",
    2: "simple instruction following",
    3: "long-horizon instruction following",
    0: "uncategorised",
}


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def main() -> int:
    files = sorted(RESULTS.glob("bench_*.json"))
    # main.py's default --out is bench_results.json, which the glob also
    # matches -- so a default-out sweep re-running challenges already saved
    # per-map would count every episode twice, inflating numerator and
    # denominator together. When per-map files exist, the default file is
    # skipped loudly; when it is all there is, it is used as-is.
    per_map = [f for f in files if f.name != "bench_results.json"]
    if per_map and len(per_map) != len(files):
        print("skipping bench_results.json (main.py's default --out): "
              "per-map files cover the same challenges")
        files = per_map
    if not files:
        print(f"no bench_*.json in {RESULTS}")
        return 1

    rows = []
    for f in files:
        try:
            rows.extend(json.loads(f.read_text()))
        except Exception as exc:  # noqa: BLE001
            print(f"skipping {f.name}: {exc}")

    # Categories come from the challenge files, not from the results, so a
    # results file written before categories existed still reports correctly.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))
    from mars_sim_driver.challenges import load_challenges

    from runner import sources

    cat, titles = {}, {}
    for _name, (_assets, root) in sources().items():
        for cid, ch in load_challenges([root]).items():
            cat[cid] = ch.category
            titles[cid] = ch.title

    # --- gate ---------------------------------------------------------------
    by_ch: dict[str, list] = {}
    for r in rows:
        by_ch.setdefault(r["challenge"], []).append(r)

    valid, invalid = set(), {}
    for cid, eps in by_ch.items():
        oracle = next((e for e in eps if e["agent"] == "oracle"), None)
        rnd = [e for e in eps if e["agent"] == "random"]
        if not rnd or oracle is None:
            invalid[cid] = "not all agents ran"
        elif any(e["passed"] for e in rnd):
            invalid[cid] = "random passed"
        elif not oracle["passed"]:
            invalid[cid] = oracle.get("error") or oracle.get("reason") or "oracle failed"
        else:
            valid.add(cid)

    print(f"=== {len(by_ch)} challenges from {len(files)} map file(s) ===")
    print(f"  VALID {len(valid)}   INVALID {len(invalid)}")
    for cid, why in sorted(invalid.items()):
        print(f"    INVALID  {cid:<28} {why}")

    # --- per-category, per-agent -------------------------------------------
    ref = {e["challenge"]: e for e in rows if e["agent"] == "oracle" and e["passed"]}
    for agent in sorted({r["agent"] for r in rows} - {"random"}):
        eps_all = [e for e in rows if e["agent"] == agent and e["challenge"] in valid]
        if not eps_all:
            continue
        print(f"\n=== {agent} ===")
        print(f"  {'category':<38} {'pass':>7}  {'goals':>9}  {'time':>6} {'turns':>6} {'path':>6}")
        for c in (1, 2, 3, 0):
            eps = [e for e in eps_all if cat.get(e["challenge"], 0) == c]
            if not eps:
                continue

            def ratio(key, eps=eps):
                vals = [e[key] / ref[e["challenge"]][key]
                        for e in eps
                        if e["challenge"] in ref and ref[e["challenge"]].get(key, 0) and e.get(key, 0)]
                m = median(vals)
                return f"{m:.2f}x" if m is not None else "-"

            print(f"  {CATEGORY_NAMES[c]:<38} "
                  f"{sum(1 for e in eps if e['passed']):>3}/{len(eps):<3} "
                  f"{sum(e['goals_done'] for e in eps):>4}/{sum(e['goals_total'] for e in eps):<4} "
                  f"{ratio('elapsed_s'):>6} {ratio('turns'):>6} {ratio('path_len_m'):>6}")
        print(f"  {'-' * 38} {sum(1 for e in eps_all if e['passed']):>3}/{len(eps_all):<3} "
              f"{sum(e['goals_done'] for e in eps_all):>4}/{sum(e['goals_total'] for e in eps_all):<4}")
        # A blind episode is not a result. Say so loudly and next to the score
        # it would otherwise silently depress -- the whole point of counting
        # camera failures was that somebody sees the count.
        blind = [e for e in eps_all if e.get("camera_errors", 0)]
        if blind:
            print(f"  !! {len(blind)} episode(s) had CAMERA FAILURES -- those scores are "
                  f"not perception results:")
            for e in sorted(blind, key=lambda x: x["challenge"])[:6]:
                print(f"       {e['challenge']:<28} {e['camera_errors']} failed frame(s)")

    # --- the suite's own shape ---------------------------------------------
    print("\n=== suite ===")
    for c in (1, 2, 3, 0):
        ids = sorted(cid for cid in by_ch if cat.get(cid, 0) == c)
        if ids:
            print(f"  {CATEGORY_NAMES[c]:<38} {len(ids):>2}   {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
