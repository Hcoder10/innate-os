# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Retention policy for simulator-image ghcr package versions.
#
# Input: the full array of package versions from
#   /orgs/<org>/packages/container/<package>/versions
# Args:
#   $heads      -- JSON array of live branch-head shas (see the workflow)
#   $main_hash  -- main's current image-inputs hash (pins inputs-<hash>)
#   $head_days  -- retention days for versions built at a live branch head
#   $other_days -- retention days for everything else
# Output: {keep: <count>, delete: [{id, tags}]}
#
# Tested by ci/test_sim_image_retention.sh -- run it after any edit here.
# Called by .github/workflows/cleanup-sim-images.yml.

def tags: .metadata.container.tags // [];
def age_days: (now - (.updated_at | fromdateiso8601)) / 86400;
def keep_forever:
  tags | any(
    . == "main" or startswith("main-")
    or startswith("buildcache")
    or . == "inputs-\($main_hash)" or startswith("inputs-\($main_hash)-")
  );
def head_of_branch:
  tags | any(startswith("sha-") and (.[4:16] as $s | $heads | index($s)));
def keep:
  keep_forever
  or (head_of_branch and age_days <= $head_days)
  or ((head_of_branch | not) and age_days <= $other_days);

{
  keep: ([.[] | select(keep)] | length),
  delete: [.[] | select(keep | not) | {id, tags: (tags | join(","))}]
}
