#!/usr/bin/env python3
"""The parser cases that have actually bitten, as a regression test.

Every case here is a real reply shape observed from a model, not an invented
one. The first is the one that made a well-formed answer look like an agent
failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backends import _coerce, _last_json_object  # noqa: E402

CASES = [
    (
        "braces inside the escaped args string",
        'codex\n{"action":"say","args":"{\\"text\\":\\"I cannot see anything, so I cannot count the cups.\\"}"}\ntokens used\n1234',
        {"action": "say", "args": {"text": "I cannot see anything, so I cannot count the cups."}},
    ),
    (
        "plain nested object instead of a string",
        '{"action":"forward","args":{"metres":0.8}}',
        {"action": "forward", "args": {"metres": 0.8}},
    ),
    (
        "preamble containing its own JSON without an action",
        '{"model":"gpt"}\nthinking...\n{"action":"turn","args":"{\\"degrees\\": -45}"}',
        {"action": "turn", "args": {"degrees": -45}},
    ),
    (
        "empty args",
        '{"action":"pick","args":""}',
        {"action": "pick", "args": {}},
    ),
    (
        "a bare number where JSON was asked for",
        '{"action":"turn","args":"90"}',
        {"action": "turn", "args": {"degrees": 90.0, "metres": 90.0}},
    ),
    (
        "uppercase action name",
        '{"action":"FINISH","args":"{}"}',
        {"action": "finish", "args": {}},
    ),
]


def main() -> int:
    bad = 0
    for name, raw, want in CASES:
        obj = _last_json_object(raw)
        got = _coerce(obj) if obj else None
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"          want {want}\n          got  {got}")

    # And the case that must NOT parse: no action anywhere.
    none_case = _last_json_object('{"note":"no action here"}')
    ok = none_case is None
    bad += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  a reply with no action returns None")

    print(f"\n{'all parser cases pass' if not bad else f'{bad} failed'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
