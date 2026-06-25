#!/usr/bin/env python3
"""Verify every install(PROGRAMS ...) node script is executable.

The exec bit on these scripts is load-bearing. A --symlink-install build (what
the local sim launcher uses) symlinks the installed executable straight at the
source file, so a source script tracked 0644 fails to launch with "executable
not found on the libexec directory". A plain copy install masks this by
installing 0755 copies, so the regression only surfaces on local sim rebuilds.

The launch tests in run_integration_tests.sh only start brain_client nodes, so
they cannot catch a lost exec bit in the mars_* / manipulation packages. This
guard checks the source script behind every install(PROGRAMS) entry across all
packages, so any such regression fails CI directly.
"""

import os
import re
import sys

SRC_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "ros2_ws", "src"))

# Keywords that terminate the file list inside install(PROGRAMS <files...> ...).
STOP_KEYWORDS = {
    "DESTINATION",
    "PERMISSIONS",
    "CONFIGURATIONS",
    "COMPONENT",
    "RENAME",
    "OPTIONAL",
    "EXCLUDE_FROM_ALL",
    "TYPE",
}

# install(...) blocks. Paths in this repo never contain ')', and CMake install()
# args do not nest parens, so a non-greedy match to the first ')' is safe.
INSTALL_RE = re.compile(r"install\s*\((.*?)\)", re.DOTALL)


def program_files(cmake_text):
    for body in INSTALL_RE.findall(cmake_text):
        tokens = body.split()
        if "PROGRAMS" not in tokens:
            continue
        for tok in tokens[tokens.index("PROGRAMS") + 1 :]:
            if tok in STOP_KEYWORDS:
                break
            yield tok


def main():
    failures = []
    checked = 0
    for dirpath, _, filenames in os.walk(SRC_ROOT):
        if "CMakeLists.txt" not in filenames:
            continue
        cmake = os.path.join(dirpath, "CMakeLists.txt")
        with open(cmake) as f:
            text = f.read()
        for rel in program_files(text):
            path = os.path.normpath(os.path.join(dirpath, rel))
            checked += 1
            if not os.path.isfile(path):
                failures.append(f"{path}: listed in {cmake} but does not exist")
            elif not os.access(path, os.X_OK):
                rel_cmake = os.path.relpath(cmake, SRC_ROOT)
                failures.append(f"{path}: missing exec bit (install(PROGRAMS) in {rel_cmake})")

    if failures:
        print("install(PROGRAMS) node scripts are not executable:", file=sys.stderr)
        for msg in failures:
            print(f"  {msg}", file=sys.stderr)
        print(
            "\nA --symlink-install build symlinks these straight at the source, so "
            "ros2 launch fails to start them. Run: chmod +x <path>",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {checked} install(PROGRAMS) node scripts are executable")


if __name__ == "__main__":
    main()
