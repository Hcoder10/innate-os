#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key.strip()] = value
    return env


def build_runtime_env(repo_root: Path) -> dict[str, str]:
    """Merged runtime environment.

    Non-secret robot tunables now live in ``innate_config/overrides.yaml``
    (delivered as ROS parameters); this reflects only the ``.env`` secrets/env.
    """
    return parse_env_file(repo_root / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print merged Innate OS runtime environment.")
    parser.add_argument("--shell", action="store_true", help="Print shell export commands")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = build_runtime_env(repo_root)

    if args.shell:
        print("; ".join(f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())))
        return 0

    for key, value in sorted(env.items()):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
