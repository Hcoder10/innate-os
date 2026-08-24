#!/usr/bin/env python3
"""What the run has cost so far, from Google's own token counts.

Reads workspace/gemini_usage.jsonl, which the brain's transport writes one line
per call (sim/bench/CHANGES.md, patch_usage_meter). These are measured, not
modelled: the counts come back on every response, so nothing here is an
estimate except the price table.

Prices per 1M tokens, gemini-3.6-flash paid tier, valid through 2026-12-31.
Thinking tokens bill as output. Update PRICES if the model changes -- and note
that this file is the only place a price is written down, so a wrong number
here is a wrong number everywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOG = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/sarta/innate-os/workspace/gemini_usage.jsonl")

PRICES = {  # $ per 1M tokens: (input, output)
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3-flash-preview": (0.50, 3.00),
}
DEFAULT_PRICE = (0.75, 3.75)


def main() -> int:
    if not LOG.exists():
        print(f"no usage log at {LOG} -- no Gemini calls recorded yet")
        return 0

    rows = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not rows:
        print("usage log is empty")
        return 0

    by_model: dict[str, dict[str, int]] = {}
    for r in rows:
        m = by_model.setdefault(r.get("model", "?"), {"calls": 0, "prompt": 0, "cached": 0, "out": 0})
        m["calls"] += 1
        m["prompt"] += int(r.get("prompt", 0))
        m["cached"] += int(r.get("cached", 0))
        # Thinking bills as output, so it belongs on that side of the ledger.
        m["out"] += int(r.get("thoughts", 0)) + int(r.get("output", 0))

    print(f"{'model':<26} {'calls':>6} {'in tok':>11} {'out tok':>9} {'cost':>9}")
    print("-" * 66)
    grand = 0.0
    calls = 0
    for model, m in sorted(by_model.items()):
        pin, pout = PRICES.get(model, DEFAULT_PRICE)
        cost = m["prompt"] * pin / 1e6 + m["out"] * pout / 1e6
        grand += cost
        calls += m["calls"]
        print(f"{model:<26} {m['calls']:>6} {m['prompt']:>11,} {m['out']:>9,} {cost:>8.4f}")
    print("-" * 66)
    print(f"{'TOTAL':<26} {calls:>6} {'':>11} {'':>9} ${grand:>7.4f}")
    if calls:
        print(f"\nper call: ${grand / calls:.5f}   "
              f"({sum(m['prompt'] for m in by_model.values()) // calls:,} in / "
              f"{sum(m['out'] for m in by_model.values()) // calls:,} out avg)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
