#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

# Service-key fallback (written by post_update.sh) so INNATE_SERVICE_KEY survives a
# repo reset. The repo .env is merged on top and wins.
SYSTEM_ENV_PATH = Path("/etc/innate.env")


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        text = path.read_text()
    except OSError as e:
        # Unreadable for any reason: treat as "no env file" rather than crashing.
        print(f"[print_runtime_env] Could not read {path}: {e}", file=sys.stderr)
        return env
    for raw_line in text.splitlines():
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
    # /etc/innate.env is the fallback; the repo .env layers on top and wins.
    env = parse_env_file(SYSTEM_ENV_PATH)
    env.update(parse_env_file(repo_root / ".env"))
    return env


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
