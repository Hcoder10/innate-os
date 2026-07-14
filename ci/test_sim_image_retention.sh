#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Fixture test for ci/sim_image_retention.jq. Exits non-zero on any
# classification mismatch. Run it after any policy edit.
set -euo pipefail
cd "$(dirname "$0")"

iso() { date -u -d "-$1 days" +%Y-%m-%dT%H:%M:%SZ; }

v() { # id, age-days, tags...
  local id="$1" age="$2"
  shift 2
  jq -n --argjson id "$id" --arg ts "$(iso "$age")" \
    '{id: $id, updated_at: $ts, metadata: {container: {tags: $ARGS.positional}}}' \
    --args "$@"
}

versions="$(
  {
    v 1 400 main sha-aaaaaaaaaaaa inputs-otherhash   # main index -> keep
    v 2 400 main-amd64 sha-aaaaaaaaaaaa-amd64        # main child -> keep
    v 3 400 buildcache-arm64                         # build cache -> keep
    v 4 40 sha-ffffffffffff inputs-MAINHASH          # stolen main-hash tag, dead branch -> keep (pin)
    v 5 40 inputs-MAINHASH-amd64 sha-ffffffffffff-amd64  # its child -> keep (pin prefix)
    v 6 20 sha-bbbbbbbbbbbb manual-bbbbbbbbbbbb      # branch head, 20d -> keep
    v 7 40 sha-cccccccccccc                          # branch head, 40d -> DELETE
    v 8 10 sha-dddddddddddd                          # not a head, 10d -> keep
    v 9 20 sha-eeeeeeeeeeee-arm64                    # not a head, 20d -> DELETE
    v 10 20                                          # untagged, 20d -> DELETE
    v 11 1                                           # untagged, fresh -> keep
  } | jq -s .
)"

expected_deleted="7 9 10"

deleted="$(
  jq -f sim_image_retention.jq \
    --argjson heads '["aaaaaaaaaaaa","bbbbbbbbbbbb","cccccccccccc"]' \
    --arg main_hash "MAINHASH" \
    --argjson head_days 30 \
    --argjson other_days 14 \
    <<<"$versions" \
  | jq -r '[.delete[].id] | sort | join(" ")'
)"

if [[ "$deleted" != "$expected_deleted" ]]; then
  echo "FAIL: deleted ids [$deleted], expected [$expected_deleted]" >&2
  exit 1
fi
echo "OK: retention policy fixture passed (deleted: $deleted)"
