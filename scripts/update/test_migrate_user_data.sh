#!/usr/bin/env bash
# Upgrade-migration test.
#
# For each historical release tag, this:
#   1. checks the tag out into a throwaway git worktree,
#   2. seeds realistic *untracked* user data in that release's on-disk layout
#      (custom skill + trained model, custom agent, legacy directive, legacy
#      primitive model, custom input, SLAM map, last mode/map),
#   3. checks out the current code on top — mirroring `innate update`, where
#      `git stash` (no -u) leaves untracked user data in the tree,
#   4. runs the REAL migration shipped in the current code
#      (scripts/update/migrate_user_data.sh), and
#   5. asserts every file landed byte-identical in the location the current
#      code scans, that old locations are cleared, and that a second run is a
#      no-op (idempotent).
#
# Usage:
#   scripts/update/test_migrate_user_data.sh [tag ...]
# Defaults to a representative set of supported tags (>= 0.2.0).
#
# Exits non-zero if any assertion fails.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
NEW_REF="$(git rev-parse HEAD)"

TAGS=("$@")
if [ ${#TAGS[@]} -eq 0 ]; then
    TAGS=(0.2.0 0.3.0 0.4.0 0.5.0 0.5.1)
fi

PASS=0
FAIL=0
WORKTREES=()

cleanup() {
    local wt
    for wt in "${WORKTREES[@]:-}"; do
        [ -n "$wt" ] || continue
        git -C "$REPO_ROOT" worktree remove --force "$wt" 2>/dev/null || true
        rm -rf "$wt" 2>/dev/null || true
    done
}
trap cleanup EXIT

ok()   { echo "  PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }

assert_same() { # <expected_file> <actual_path> <label>
    if [ ! -f "$2" ]; then
        fail "$3 (missing: $2)"
    elif cmp -s "$1" "$2"; then
        ok "$3"
    else
        fail "$3 (content differs: $2)"
    fi
}

assert_absent() { # <path> <label>
    if [ -e "$1" ]; then fail "$2 (still present: $1)"; else ok "$2"; fi
}

seed_user_data() { # <worktree>
    local wt="$1"
    mkdir -p "$wt/skills/my_pick_skill" "$wt/agents" "$wt/directives" \
             "$wt/primitives/legacy_wave" "$wt/inputs" "$wt/maps"
    head -c 2048 /dev/urandom > "$wt/skills/my_pick_skill/model.h5"
    printf '# my custom skill\n'      > "$wt/skills/my_pick_skill/skill.py"
    printf '# my custom agent\n'      > "$wt/agents/my_agent.py"
    printf '# my legacy directive\n'  > "$wt/directives/my_directive.py"
    head -c 4096 /dev/urandom > "$wt/primitives/legacy_wave/policy.h5"
    printf '# my custom input\n'      > "$wt/inputs/my_input.py"
    head -c 8192 /dev/urandom > "$wt/maps/home.pgm"
    printf 'image: home.pgm\nresolution: 0.05\norigin: [0,0,0]\n' > "$wt/maps/home.yaml"
    printf 'navigation' > "$wt/.last_mode"
    printf 'home.yaml'  > "$wt/.last_map"
}

snapshot() { # <worktree> <snapdir>
    local wt="$1" s="$2"
    mkdir -p "$s"
    cp "$wt/skills/my_pick_skill/model.h5"   "$s/model.h5"
    cp "$wt/skills/my_pick_skill/skill.py"   "$s/skill.py"
    cp "$wt/agents/my_agent.py"              "$s/my_agent.py"
    cp "$wt/directives/my_directive.py"      "$s/my_directive.py"
    cp "$wt/primitives/legacy_wave/policy.h5" "$s/policy.h5"
    cp "$wt/inputs/my_input.py"              "$s/my_input.py"
    cp "$wt/maps/home.pgm"                   "$s/home.pgm"
    cp "$wt/maps/home.yaml"                  "$s/home.yaml"
    cp "$wt/.last_mode"                      "$s/.last_mode"
    cp "$wt/.last_map"                       "$s/.last_map"
}

assert_landed() { # <worktree> <snapdir> <tag>
    local wt="$1" s="$2" tag="$3"
    assert_same "$s/model.h5"        "$wt/workspace/custom_skills/my_pick_skill/model.h5"   "$tag: trained model -> custom_skills"
    assert_same "$s/skill.py"        "$wt/workspace/custom_skills/my_pick_skill/skill.py"   "$tag: skill code -> custom_skills"
    assert_same "$s/my_agent.py"     "$wt/workspace/custom_agents/my_agent.py"              "$tag: agent -> custom_agents"
    assert_same "$s/my_directive.py" "$wt/workspace/custom_agents/my_directive.py"          "$tag: legacy directive -> custom_agents"
    assert_same "$s/policy.h5"       "$wt/workspace/innate_skills/legacy_wave/policy.h5"    "$tag: legacy primitive model -> innate_skills"
    assert_same "$s/my_input.py"     "$wt/workspace/inputs/my_input.py"                     "$tag: input -> workspace/inputs"
    assert_same "$s/home.pgm"        "$wt/data/maps/home.pgm"                               "$tag: SLAM map (pgm) -> data/maps"
    assert_same "$s/home.yaml"       "$wt/data/maps/home.yaml"                              "$tag: SLAM map (yaml) -> data/maps"
    assert_same "$s/.last_mode"      "$wt/data/.last_mode"                                  "$tag: .last_mode -> data/"
    assert_same "$s/.last_map"       "$wt/data/.last_map"                                   "$tag: .last_map -> data/"
}

for TAG in "${TAGS[@]}"; do
    echo "=================================================================="
    echo "TAG $TAG  ->  $NEW_REF"
    echo "=================================================================="

    WT="$(mktemp -d)/wt-$TAG"
    if ! git -C "$REPO_ROOT" worktree add --quiet --detach "$WT" "$TAG"; then
        fail "$TAG: could not create worktree at tag"
        continue
    fi
    WORKTREES+=("$WT")

    seed_user_data "$WT"
    SNAP="$(mktemp -d)"
    snapshot "$WT" "$SNAP"

    # Simulate the upgrade: check out the current code over the seeded tree.
    if ! git -C "$WT" checkout --quiet "$NEW_REF" 2>/dev/null; then
        fail "$TAG: checkout to new ref failed (untracked-file collision?)"
        continue
    fi

    # Run the real migration shipped in the code under test (this checkout),
    # applied to the post-upgrade on-disk state we just produced.
    MIGRATE="$REPO_ROOT/scripts/update/migrate_user_data.sh"
    if [ ! -f "$MIGRATE" ]; then
        fail "$TAG: migrate_user_data.sh missing at $MIGRATE"
        continue
    fi

    # Run the real migration (first pass).
    if ! bash "$MIGRATE" "$WT" > /dev/null; then
        fail "$TAG: migration exited non-zero (first pass)"
    fi

    assert_landed "$WT" "$SNAP" "$TAG"
    assert_absent "$WT/maps/home.yaml"                        "$TAG: old maps/ cleared"
    assert_absent "$WT/.last_mode"                            "$TAG: old .last_mode cleared"
    assert_absent "$WT/primitives/legacy_wave/policy.h5"      "$TAG: old primitives model cleared"
    assert_absent "$WT/agents/my_agent.py"                    "$TAG: old agents/ cleared"

    # Idempotency: a second pass must not error or alter the migrated data.
    if ! bash "$MIGRATE" "$WT" > /dev/null; then
        fail "$TAG: migration exited non-zero (second pass / idempotency)"
    fi
    assert_same "$SNAP/home.yaml" "$WT/data/maps/home.yaml"                              "$TAG: idempotent (map intact)"
    assert_same "$SNAP/model.h5"  "$WT/workspace/custom_skills/my_pick_skill/model.h5"  "$TAG: idempotent (model intact)"

    rm -rf "$SNAP"
done

echo "=================================================================="
echo "RESULT: $PASS passed, $FAIL failed"
echo "=================================================================="
[ "$FAIL" -eq 0 ]
