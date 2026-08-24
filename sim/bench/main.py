#!/usr/bin/env python3
"""Run the benchmark: every challenge, every agent, in parallel.

    python sim/bench/main.py                      # everything + validity gate
    python sim/bench/main.py --map rounds
    python sim/bench/main.py --map apartment --agents oracle

THE VALIDITY GATE is the point of this file. A challenge only counts if

    the oracle PASSES   -- it is solvable at all, so a failure is the agent's
    the random FAILS    -- it is not solvable by flailing, so a pass means something

ARC-AGI-3 screened 414 candidate environments down to 135 on the second rule
alone. A suite that has not been screened reports numbers that look like
capability and are not.

NOT EVERY CHALLENGE CAN BE ORACLED. Anything gated on SkillDone needs the arm
and the brain's skill events; a scripted base agent has neither. Those are
reported NEEDS-ARM and held to the weaker half of the rule (random must still
fail), which is stated rather than quietly folded into the pass count.

ONE EPISODE PER PROCESS is deliberate: core.py reads ASSETS_DIR at import time,
so a worker that ran a second episode for a different map would silently keep
building the first map's world.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import Episode, run_episode, sources  # noqa: E402


def discover() -> dict[str, list[tuple[str, str]]]:
    """{map: [(challenge_id, requirement)]} without building any world."""
    from mars_sim_driver.challenges import load_challenges

    import autoplan

    out: dict[str, list[tuple[str, str]]] = {}
    for name, (_assets, root) in sources().items():
        found = load_challenges([root])
        out[name] = sorted((cid, autoplan.classify(ch)) for cid, ch in found.items())
        CATEGORY_OF.update({cid: ch.category for cid, ch in found.items()})
    return out


# Results live beside the harness, not in /tmp. A sweep runs hot enough that
# systemd-tmpfiles cleaned /tmp mid-run twice, taking the results file and the
# per-episode progress directory with it -- and a worker that cannot write its
# progress file fails the episode for a reason that has nothing to do with the
# robot.
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# challenge id -> category, filled by discover(). A module-level dict rather
# than a return value because discover() already has a shape every caller
# depends on, and the categories are wanted in exactly one place.
CATEGORY_OF: dict[str, int] = {}


CATEGORY_NAMES = {
    1: "easy observation and conversation",
    2: "simple instruction following",
    3: "long-horizon instruction following",
    0: "uncategorised",
}


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def scorecard(results, categories: dict, valid: set, agent: str) -> None:
    """One line per category for one agent, against the reference plan.

    Ratios, not absolutes: seconds and metres are properties of the map, and
    the ratio to the oracle on the SAME challenge is a property of the agent.
    """
    ref = {e.challenge: e for e in results if e.agent == "oracle" and e.passed}
    rows = [e for e in results if e.agent == agent and e.challenge in valid]
    if not rows:
        return

    print(f"\n=== scorecard: {agent} ===")
    print(f"  {'category':<38} {'pass':>7}  {'goals':>9}  {'time':>6} {'turns':>6} {'path':>6}")
    for cat in (1, 2, 3, 0):
        eps = [e for e in rows if categories.get(e.challenge, 0) == cat]
        if not eps:
            continue
        done = sum(e.goals_done for e in eps)
        avail = sum(e.goals_total for e in eps)
        passed = sum(1 for e in eps if e.passed)

        def ratio(get) -> str:
            # Only challenges where BOTH this agent and the reference produced
            # a number. A ratio against a missing denominator is not a number.
            vals = [get(e) / get(ref[e.challenge])
                    for e in eps
                    if e.challenge in ref and get(ref[e.challenge]) > 0 and get(e) > 0]
            return f"{_median(vals):.2f}x" if vals else "-"

        print(f"  {CATEGORY_NAMES[cat]:<38} {passed:>3}/{len(eps):<3} "
              f"{done:>4}/{avail:<4} {ratio(lambda e: e.elapsed_s):>6} "
              f"{ratio(lambda e: float(e.turns)):>6} {ratio(lambda e: e.path_len_m):>6}")

    done = sum(e.goals_done for e in rows)
    avail = sum(e.goals_total for e in rows)
    passed = sum(1 for e in rows if e.passed)
    print(f"  {'-' * 38} {passed:>3}/{len(rows):<3} {done:>4}/{avail:<4}")


def _one(job):
    map_name, challenge_id, agent_name, seed, cap = job
    import autoplan
    from agents import RandomAgent
    from planner_agent import PlannerAgent

    def make(ch):
        if agent_name == "random":
            return RandomAgent(seed=seed)
        # "brain:codex", "brain:gemini", "brain:echo" -- the backend is the
        # agent architecture, and swapping it is the point of the seam.
        if agent_name.startswith("brain"):
            from backends import BACKENDS
            from brain_agent import BrainAgent

            # backends_v2 is a SEPARATE registry (AGENT_SPEC.md's new agent)
            # merged in here rather than into backends.py itself, so the
            # extension point stays visible as an addition, not an edit --
            # this dict merge is the entirety of what "point the harness at
            # a different architecture" costs.
            from backends_v2 import BACKENDS_V2
            from backends_v3 import BACKENDS_V3
            from backends_v4 import BACKENDS_V4
            all_backends = {**BACKENDS, **BACKENDS_V2, **BACKENDS_V3, **BACKENDS_V4}

            kind = agent_name.split(":", 1)[1] if ":" in agent_name else "codex"
            if kind not in all_backends:
                raise ValueError(f"unknown backend {kind!r}; have {sorted(all_backends)}")
            agent = BrainAgent(all_backends[kind]())
            # The report has to be able to say WHICH backend produced a number.
            # "brain" alone would silently merge a vision run and a blind one.
            agent.name = agent_name
            # Same formula claude_bridge.py already uses for the live path --
            # this in-process path was missing it entirely, meaning every
            # challenge ran at BrainAgent's flat class-default of 40 turns
            # regardless of its own time_limit_s. Found by tracing a real
            # counter_within_reach episode (FINDINGS.md T19): it hit turn 40
            # mid-"pick", with no explicit finish ever called and the model
            # still visibly adapting (shrinking its forward distances turn by
            # turn, with real successes in the back half) -- the episode did
            # not end because the agent gave up, it ended because the harness
            # ran out of turns to give it, on a challenge whose OWN 420 s time
            # budget scales to 46 by this same formula. The turn cap exists to
            # stop loops, not to bind before the sim clock does -- see
            # claude_bridge.py's identical comment -- and every challenge with
            # a longer time_limit_s than ~360 s (40 * 9) was silently turn-
            # capped tighter than its own stated budget in every in-process
            # sweep this project has run, including the numbers in
            # NEMOTRON_STACK_RESULTS.md.
            agent.max_turns = max(40, int((ch.time_limit_s or 400) / 9))
            return agent
        # A hand-written plan wins where one exists. Deriving a plan from goals
        # gets the route right but not the approach: a hand plan encodes which
        # SIDE of a bench to stand on and how to line up on a 0.45 m doorway,
        # which no amount of reading the goal tells you. Both run through the
        # same A* follower, so a hand plan is a sequence of hints, not a route.
        from oracles import ORACLES

        steps = ORACLES.get(ch.id) or autoplan.plan_for(ch)
        if steps is None:
            raise ValueError(f"no auto-plan: {autoplan.classify(ch)}")
        return PlannerAgent(steps)

    try:
        wh = (640, 480) if agent_name.startswith("brain") else (160, 120)
        return run_episode(map_name, challenge_id, make, max_sim_s=cap, render_wh=wh)
    except Exception as exc:  # noqa: BLE001 -- one bad episode must not sink the sweep
        return Episode(map_name, challenge_id, agent_name, False, 0, 0, 0.0, "", 0.0, 0,
                       error=f"{type(exc).__name__}: {exc}")


def rhae(agent_steps: int, reference_steps: int) -> float:
    """ARC-AGI-3's efficiency score, S = min(1, h/a)^2.

    h there is the SECOND-BEST human's action count. No human data has been
    collected here, so the oracle's step count stands in and this is a score
    against a reference plan, not against people. Reported as such; do not
    quote it as an ARC-AGI-3 number.
    """
    if agent_steps <= 0:
        return 0.0
    return min(1.0, reference_steps / agent_steps) ** 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="append")
    ap.add_argument("--agents", default="oracle,random")
    ap.add_argument("--workers", type=int, default=0, help="0 = cpu_count - 2")
    ap.add_argument("--seeds", type=int, default=1, help="random-agent rollouts per challenge")
    ap.add_argument("--random-cap", type=float, default=240.0,
                    help="sim-seconds budget for random rollouts (0 = the challenge's own limit)")
    ap.add_argument("--limit", type=int, default=0, help="cap challenges per map (smoke tests)")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "bench_results.json")
    args = ap.parse_args()

    catalogue = discover()
    # Default to the benchmark's own eight bundles. The apartment root also
    # loads 77 legacy, uncategorised challenges, and sweeping them silently
    # under "run everything" turns the 45-episode suite into 122 episodes
    # whose extra rows all land in the uncategorised bucket. They stay
    # runnable -- --map apartment -- but only on request.
    maps = args.map or sorted(m for m in catalogue if m != "apartment")
    agents = args.agents.split(",")
    cap = args.random_cap or None

    jobs, needs_arm = [], {}
    for m in maps:
        entries = catalogue.get(m, [])
        if args.limit:
            entries = entries[: args.limit]
        for cid, req in entries:
            needs_arm[cid] = req
            if "oracle" in agents and req not in ("arm", "unknown"):
                jobs.append((m, cid, "oracle", 0, None))
            for a in agents:
                # An agent under test never joins the gate that decides whether
                # its own test is fair: the gate is oracle-and-random only.
                if a.startswith("brain"):
                    jobs.append((m, cid, a, 0, None))
            if "random" in agents:
                jobs.extend((m, cid, "random", s, cap) for s in range(args.seeds))

    total_ch = sum(len(catalogue.get(m, [])[: args.limit] if args.limit else catalogue.get(m, [])) for m in maps)
    workers = args.workers or max(1, min(len(jobs), (mp.cpu_count() or 4) - 2))
    print(f"{total_ch} challenges across {len(maps)} maps -> {len(jobs)} episodes on {workers} workers")
    skipped = sum(1 for r in needs_arm.values() if r in ("arm", "unknown"))
    if skipped:
        print(f"{skipped} challenges have no auto-plan (SkillDone / unmodelled) -- random-only")
    if cap:
        # Never let a bound on coverage go unstated: a random rollout that is
        # cut off has not been proven to fail, only shown not to succeed inside
        # this budget. Oracles always run to the challenge's own limit.
        print(f"random rollouts capped at {cap:g} sim-seconds (oracles uncapped)")
    print()

    # imap_unordered, not map: results stream as they land and are written out
    # incrementally, so a sweep that is killed part-way still leaves data.
    results = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with mp.Pool(workers, maxtasksperchild=1) as pool:
        for e in pool.imap_unordered(_one, jobs):
            e.needs = needs_arm.get(e.challenge, "")
            results.append(e)
            print(f"[{len(results):>3}/{len(jobs)}] {e.as_row()}", flush=True)
            args.out.write_text(json.dumps([asdict(r) for r in results], indent=1))

    # --- validity gate ---
    print("\n=== validity gate ===")
    tally = {"VALID": 0, "NEEDS-ARM": 0, "INVALID": 0, "INCOMPLETE": 0}
    valid_ids: set[str] = set()
    for m in maps:
        for cid, req in (catalogue.get(m, [])[: args.limit] if args.limit else catalogue.get(m, [])):
            eps = [e for e in results if e.challenge == cid]
            oracle = next((e for e in eps if e.agent == "oracle"), None)
            rnd = [e for e in eps if e.agent == "random"]
            trivial = any(e.passed for e in rnd)

            if req in ("arm", "unknown"):
                verdict = "INVALID" if trivial else "NEEDS-ARM"
                why = "random passed" if trivial else f"no auto-plan ({req}); solvability unproven"
            elif not rnd or oracle is None:
                verdict, why = "INCOMPLETE", "not all agents ran"
            elif trivial:
                verdict, why = "INVALID", "random passed -- measures nothing"
            elif not oracle.passed:
                verdict = "INVALID"
                why = oracle.error or oracle.reason or f"oracle {oracle.goals_done}/{oracle.goals_total}"
            else:
                verdict, why = "VALID", ""
            tally[verdict] += 1
            if verdict == "VALID":
                valid_ids.add(cid)
            print(f"  {verdict:<10} {m:<10} {cid:<28} {why}")

    # One scorecard per agent that ran, so a sweep with several agents in it
    # reads as a comparison rather than as several unrelated tables. Only VALID
    # challenges: an INVALID one has not been shown solvable, and including it
    # would mix "the agent failed" with "nobody could".
    for a in sorted({e.agent for e in results} - {"random"}):
        scorecard(results, CATEGORY_OF, valid_ids, a)

    print("\n=== scores (oracle) ===")
    for e in sorted(results, key=lambda x: x.challenge):
        if e.agent == "oracle" and e.passed:
            print(f"  {e.challenge:<28} {e.goals_done}/{e.goals_total}  sim {e.elapsed_s:6.1f}s  "
                  f"steps {e.steps:5d}  eff {rhae(e.steps, e.steps):.2f}")

    work = sum(e.wall_s for e in results)
    print(f"\n{len(results)} episodes, {work:.0f}s of work on {workers} workers "
          f"({work / max(1, workers):.0f}s wall-equivalent)")
    print("  " + "  ".join(f"{k}={v}" for k, v in tally.items()))
    return 0 if tally["INVALID"] == 0 and tally["INCOMPLETE"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
