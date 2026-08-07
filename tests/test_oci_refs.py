# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""How an image reference splits into the repository and reference the v2 API wants.

Both halves reach the network -- the repository names the token scope AND the
manifest URL -- so a split that is wrong in either half fails as an opaque
401/404 from GHCR rather than as anything the user can act on. Digest refs are
the case worth pinning: INNATE_SIM_ASSETS_IMAGE is user-supplied and a digest
is the natural way to pin one.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "sim" / "launcher"))

IMAGE = "ghcr.io/innate-inc/innate-os-sim-assets"
REPO = "innate-inc/innate-os-sim-assets"
DIGEST = "sha256:" + "ab" * 32


def test_a_tag_ref_splits_on_the_tag():
    import oci

    assert oci.split_ref(f"{IMAGE}:main") == (REPO, "main")
    assert oci.split_ref(f"{IMAGE}:inputs-0123456789ab") == (REPO, "inputs-0123456789ab")


def test_a_digest_ref_splits_on_the_at_not_the_last_colon():
    """rpartition(':') would hand back the repository `...-sim-assets@sha256`,
    which no registry has."""
    import oci

    assert oci.split_ref(f"{IMAGE}@{DIGEST}") == (REPO, DIGEST)


def test_a_tag_and_digest_ref_pins_by_digest():
    import oci

    assert oci.split_ref(f"{IMAGE}:main@{DIGEST}") == (REPO, DIGEST)


def test_an_untagged_ref_says_so():
    """Not "names no registry" -- that describes a different mistake and sends
    the reader looking at the wrong half of their override."""
    import oci

    with pytest.raises(oci.OciError, match="no tag or digest"):
        oci.split_ref(IMAGE)


def test_a_ref_without_a_registry_still_says_so():
    import oci

    with pytest.raises(oci.OciError, match="no registry"):
        oci.split_ref("innate-os-sim-assets:main")
