# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which Docker daemons the launcher warns about before `up` fails on them.

The window is closed at both ends and patch-precise, which is the whole point:
29.1.3 breaks every `type: image` mount and 29.1.4 fixes it (moby#51687), so a
check that only reads major.minor -- like _require_min_version does -- would
warn every 29.1.x user forever. Verified against real daemons in dind: 28.4.0
and 29.1.4+ mount our 111-char asset ref fine, 29.0.0 and 29.1.3 do not, under
both the overlay2 graphdriver and the containerd snapshotter.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "sim" / "launcher"))


@pytest.fixture
def warnings(monkeypatch):
    import runtime

    emitted: list[str] = []
    monkeypatch.setattr(runtime, "warn", emitted.append)
    return emitted


@pytest.mark.parametrize("version", ["29.0.0", "29.1.0", "29.1.3"])
def test_a_broken_daemon_is_named_before_it_fails(warnings, version):
    import runtime

    runtime._warn_broken_image_mounts(version, "./innate-sim up")
    assert len(warnings) == 1
    assert version in warnings[0]
    assert "51687" in warnings[0]


@pytest.mark.parametrize("version", ["28.4.0", "29.1.4", "29.7.1", "30.0.0"])
def test_a_working_daemon_says_nothing(warnings, version):
    import runtime

    runtime._warn_broken_image_mounts(version, "./innate-sim up")
    assert warnings == []


def test_an_unparseable_version_says_nothing(warnings):
    """`docker info` answered, but not with something we recognise. An
    unrecognised build string is not evidence of a broken one, and a false
    'your Docker is broken' sends people to reinstall for nothing."""
    import runtime

    for output in ("", None, "dev", "29.1"):
        runtime._warn_broken_image_mounts(output, "./innate-sim up")
    assert warnings == []
