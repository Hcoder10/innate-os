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
# Fail safe on a missing timestamp: age 0 keeps the version this run
# instead of crashing the whole cleanup on one anomalous API object.
def age_days:
  (.updated_at // .created_at) as $ts
  | if $ts == null then 0 else (now - ($ts | fromdateiso8601)) / 86400 end;
def keep_forever:
  tags | any(
    . == "main" or startswith("main-")
    or startswith("buildcache")
    or . == "inputs-\($main_hash)" or startswith("inputs-\($main_hash)-")
  );
# Commit components of this version's sha-<hex>[-<arch>] tags. Prefix-matched
# against the full-length heads so a publish-side short-sha width change
# cannot silently break head matching.
def sha_commits:
  [tags[] | select(startswith("sha-")) | ltrimstr("sha-") | split("-")[0]
   | select(length > 0)];
def head_of_branch:
  any(sha_commits[]; . as $c | any($heads[]; startswith($c)));
def keep_own:
  keep_forever
  or (head_of_branch and age_days <= $head_days)
  or ((head_of_branch | not) and age_days <= $other_days);

# A kept multi-arch index references its per-arch children by DIGEST, but
# children are protected only by movable tags that the next build steals
# (main-amd64, inputs-<h>-amd64, buildcache-amd64). Index and children
# always share their commit's sha- tag, so versions sharing a commit with
# any kept version are kept too -- a kept index can never outlive the
# children it references.
. as $versions
| ([$versions[] | select(keep_own) | sha_commits[]] | unique) as $kept_commits
| [$versions[]
   | . + {_keep: (keep_own
                  or any(sha_commits[]; . as $c | $kept_commits | index($c)))}]
| {
  keep: (map(select(._keep)) | length),
  delete: [.[] | select(._keep | not) | {id, tags: (tags | join(","))}]
}
