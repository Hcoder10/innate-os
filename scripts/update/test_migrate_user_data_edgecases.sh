#!/usr/bin/env bash
# Behavioral / edge-case tests for the migration library, complementing the
# cross-version matrix in test_migrate_user_data.sh. These exercise migration
# semantics that don't depend on the source tag:
#   1. .pt / .pth model files migrate (not just .h5)
#   2. name collision never overwrites and preserves the source
#   3. symlinked dataset dirs are preserved as symlinks
#   4. multi-file skill dirs migrate whole (model + metadata + data/)
#   5. the exact production invocation (sourced, set -e, log() defined,
#      MIGRATE_CHOWN_USER set) completes and routes logs correctly
#   6. the `innate update` git flow (stash push without -u, then checkout)
#      preserves untracked user data and is not blocked by tracked edits
#
# Exits non-zero if any assertion fails.

set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
NEW_REF="$(git rev-parse HEAD)"
MIGRATE="$REPO_ROOT/scripts/update/migrate_user_data.sh"

PASS=0
FAIL=0
TMP_ROOTS=()
WORKTREES=()

cleanup() {
    local p
    for p in "${WORKTREES[@]:-}"; do
        [ -n "$p" ] && git -C "$REPO_ROOT" worktree remove --force "$p" 2>/dev/null || true
    done
    for p in "${TMP_ROOTS[@]:-}"; do
        [ -n "$p" ] && rm -rf "$p" 2>/dev/null || true
    done
}
trap cleanup EXIT

ok()   { echo "  PASS: $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }

# Substring match without a pipe (avoids set -o pipefail + grep -q SIGPIPE).
contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac }

new_repo() {
    local base
    base="$(mktemp -d)"
    TMP_ROOTS+=("$base")
    mkdir -p "$base/repo"
    echo "$base/repo"
}

echo "=== 1. .pt / .pth primitive models migrate to innate_skills ==="
test_pt_pth() {
    local r
    r="$(new_repo)"
    mkdir -p "$r/primitives/policyA" "$r/primitives/policyB"
    printf 'PT' > "$r/primitives/policyA/model.pt"
    printf 'PTH' > "$r/primitives/policyB/model.pth"
    bash "$MIGRATE" "$r" > /dev/null
    [ "$(cat "$r/workspace/innate_skills/policyA/model.pt" 2>/dev/null)" = "PT" ] &&
        ok ".pt model migrated" || fail ".pt model not migrated"
    [ "$(cat "$r/workspace/innate_skills/policyB/model.pth" 2>/dev/null)" = "PTH" ] &&
        ok ".pth model migrated" || fail ".pth model not migrated"
}
test_pt_pth

echo "=== 2. name collision never overwrites, source preserved ==="
test_collision() {
    local r
    r="$(new_repo)"
    mkdir -p "$r/skills/dup" "$r/workspace/custom_skills/dup"
    printf 'NEW' > "$r/skills/dup/model.h5"
    printf 'OLD' > "$r/workspace/custom_skills/dup/model.h5"
    bash "$MIGRATE" "$r" > /dev/null
    [ "$(cat "$r/workspace/custom_skills/dup/model.h5")" = "OLD" ] &&
        ok "collision: destination NOT overwritten" || fail "collision: destination overwritten!"
    [ "$(cat "$r/skills/dup/model.h5" 2>/dev/null)" = "NEW" ] &&
        ok "collision: source preserved for manual reconcile" || fail "collision: source lost!"
}
test_collision

echo "=== 3. symlinked dataset dir preserved as a symlink ==="
test_symlink() {
    local r target
    r="$(new_repo)"
    target="$(mktemp -d)"
    TMP_ROOTS+=("$target")
    printf 'DATASET' > "$target/episodes.bin"
    mkdir -p "$r/skills/sym_skill"
    printf 'code' > "$r/skills/sym_skill/skill.py"
    ln -s "$target" "$r/skills/sym_skill/data"
    bash "$MIGRATE" "$r" > /dev/null
    local dst="$r/workspace/custom_skills/sym_skill/data"
    [ -L "$dst" ] && ok "symlink preserved as symlink" || fail "symlink not preserved"
    [ "$(cat "$dst/episodes.bin" 2>/dev/null)" = "DATASET" ] &&
        ok "symlink target still reachable" || fail "symlink target broken"
}
test_symlink

echo "=== 4. multi-file skill dir migrates whole ==="
test_multifile() {
    local r
    r="$(new_repo)"
    mkdir -p "$r/skills/pick/data"
    printf 'h5' > "$r/skills/pick/model.h5"
    printf '{"type":"physical"}' > "$r/skills/pick/metadata.json"
    printf 'eps' > "$r/skills/pick/data/dataset_metadata.json"
    bash "$MIGRATE" "$r" > /dev/null
    local d="$r/workspace/custom_skills/pick"
    if [ -f "$d/model.h5" ] && [ -f "$d/metadata.json" ] && [ -f "$d/data/dataset_metadata.json" ]; then
        ok "multi-file skill dir migrated whole (model + metadata + data/)"
    else
        fail "multi-file skill dir incomplete after migration"
    fi
}
test_multifile

echo "=== 5. production invocation (sourced, set -e, log(), MIGRATE_CHOWN_USER) ==="
test_production_invocation() {
    local r out
    r="$(new_repo)"
    mkdir -p "$r/maps" "$r/agents"
    printf 'image: home.pgm' > "$r/maps/home.yaml"
    printf 'navigation' > "$r/.last_mode"
    printf 'a' > "$r/agents/x.py"
    # Reproduce exactly how post_update.sh calls the library.
    out="$(
        set -e
        LOG_LINES=()
        log() { LOG_LINES+=("$*"); }
        # shellcheck disable=SC1090
        source "$MIGRATE"
        MIGRATE_CHOWN_USER="$(id -un)" run_user_data_migrations "$r"
        echo "MIGRATION_OK"
        joined="$(printf '%s\n' "${LOG_LINES[@]:-}")"
        case "$joined" in *data/maps*) echo "LOG_ROUTED_OK" ;; esac
    )"
    if contains "MIGRATION_OK" "$out"; then
        ok "sourced under set -e: run_user_data_migrations completes"
    else
        fail "sourced under set -e: aborted"
    fi
    if contains "LOG_ROUTED_OK" "$out"; then
        ok "log() routed via declare -F (not echoed)"
    else
        fail "log() not routed to host log()"
    fi
    [ -f "$r/data/maps/home.yaml" ] &&
        ok "production invocation: map migrated to data/maps" || fail "production invocation: map not migrated"
}
test_production_invocation

echo "=== 6. innate update git flow: stash (no -u) + checkout preserves untracked ==="
test_stash_flow() {
    local wt
    wt="$(mktemp -d)/wt"
    if ! git -C "$REPO_ROOT" worktree add --quiet --detach "$wt" 0.5.0; then
        fail "stash flow: could not create worktree"
        return
    fi
    WORKTREES+=("$wt")
    mkdir -p "$wt/maps"
    printf 'map-bytes' > "$wt/maps/home.yaml"   # untracked user data
    echo "# user edit" >> "$wt/README.md"        # modified tracked file
    # Mirror scripts/innate: stash push (no -u), then checkout.
    git -C "$wt" stash push -m "auto-stash before update" > /dev/null 2>&1 || true
    if git -C "$wt" checkout --quiet "$NEW_REF" 2>/dev/null; then
        ok "stash+checkout: upgrade not blocked"
    else
        fail "stash+checkout: blocked"
    fi
    [ "$(cat "$wt/maps/home.yaml" 2>/dev/null)" = "map-bytes" ] &&
        ok "stash flow: untracked user data survived" || fail "stash flow: untracked data lost!"
    if contains "auto-stash before update" "$(git -C "$wt" stash list 2>/dev/null)"; then
        ok "stash flow: tracked edit parked & recoverable"
    else
        fail "stash flow: tracked edit not stashed"
    fi
    # Drop the stash this test just created (top of stack); leave older ones.
    git -C "$wt" stash drop >/dev/null 2>&1 || true
}
test_stash_flow

echo "=================================================================="
echo "RESULT: $PASS passed, $FAIL failed"
echo "=================================================================="
[ "$FAIL" -eq 0 ]
