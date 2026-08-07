#!/usr/bin/env python3
"""Fail CI when a pull request adds or grows a file past the size limit.

The limit is a guardrail against accidentally committing large binaries
(build artifacts, datasets, notebooks with saved outputs, ...) into git.
Files that are intentionally large can be exempted by listing them in a
`.sizelimitignore` file at the repo root, which uses gitignore-style
patterns. Adding an entry there is the "explicit approval" — it lands in
the diff and gets reviewed like any other change.

Environment:
  MAX_BYTES            Size limit in bytes (default 102400 = 100 KiB).
  SIZE_LIMIT_IGNORE    Path to the ignore file (default .sizelimitignore).
  BASE_SHA / HEAD_SHA  Commit range to diff; only files changed in the PR
                       are checked. HEAD_SHA defaults to HEAD.
  CHANGED_FILES        Optional newline-separated file list that overrides
                       the git diff (used by the unit test).
"""
import os
import re
import subprocess
import sys


def _translate(pattern):
    """Translate a gitignore glob body into a regex fragment."""
    i, n = 0, len(pattern)
    out = ""
    while i < n:
        c = pattern[i]
        i += 1
        if c == "*":
            if i < n and pattern[i] == "*":
                i += 1
                if i < n and pattern[i] == "/":
                    i += 1
                    out += "(?:.*/)?"  # '**/' — zero or more leading dirs
                else:
                    out += ".*"        # '**' — anything, including slashes
            else:
                out += "[^/]*"          # '*' — anything but a slash
        elif c == "?":
            out += "[^/]"
        elif c == "/":
            out += "/"
        else:
            out += re.escape(c)
    return out


class SizeLimitIgnore:
    """A small gitignore-style matcher for the .sizelimitignore file."""

    def __init__(self, lines):
        self.rules = []
        for raw in lines:
            line = raw.rstrip("\n")
            line = re.sub(r"(?<!\\)\s+$", "", line)  # trailing unescaped space
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            if line[:2] in ("\\#", "\\!"):
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            anchored = "/" in line
            if line.startswith("/"):
                line = line[1:]
            body = _translate(line)
            regex = ("^" if anchored else "^(?:.*/)?") + body
            regex += "/.*$" if dir_only else "(?:/.*)?$"
            self.rules.append((re.compile(regex), negate))

    def ignored(self, path):
        result = False
        for regex, negate in self.rules:
            if regex.match(path):
                result = not negate
        return result


def _human(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def _changed_files():
    override = os.environ.get("CHANGED_FILES")
    if override is not None:
        return [ln.strip() for ln in override.splitlines() if ln.strip()]
    base = os.environ.get("BASE_SHA")
    head = os.environ.get("HEAD_SHA", "HEAD")
    if not base:
        sys.exit("BASE_SHA is required (or set CHANGED_FILES).")
    out = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=d", f"{base}...{head}"],
        text=True,
    )
    return [ln for ln in out.splitlines() if ln]


def main():
    max_bytes = int(os.environ.get("MAX_BYTES", 102400))
    ignore_path = os.environ.get("SIZE_LIMIT_IGNORE", ".sizelimitignore")

    lines = []
    if os.path.exists(ignore_path):
        with open(ignore_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    matcher = SizeLimitIgnore(lines)

    violations = []
    for path in _changed_files():
        if not os.path.isfile(path) or matcher.ignored(path):
            continue
        size = os.path.getsize(path)
        if size > max_bytes:
            violations.append((path, size))

    if not violations:
        print(f"OK — no changed file exceeds {_human(max_bytes)}.")
        return 0

    print(f"Found {len(violations)} file(s) larger than {_human(max_bytes)}:\n")
    for path, size in sorted(violations, key=lambda v: -v[1]):
        # GitHub renders ::error annotations inline on the PR's Files tab.
        print(f"::error file={path}::{path} is {_human(size)} (limit {_human(max_bytes)})")
        print(f"  {_human(size):>10}  {path}")
    print(
        "\nIf a file is intentionally large, add it to .sizelimitignore "
        "(gitignore-style patterns) in the same PR so it gets reviewed."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
