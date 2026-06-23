#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 user@robot:/home/user/innate-os"
    echo "   or: $0 /absolute/path/to/innate-os"
    exit 2
fi

target="${1%/}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skill_root="$repo_root/workspace/custom_skills"
skills=(
    "pick-socks-feb3-baseline"
    "pick-socks-feb3-sock-state"
)

for skill in "${skills[@]}"; do
    if [[ ! -f "$skill_root/$skill/metadata.json" ]]; then
        echo "Missing $skill_root/$skill/metadata.json"
        exit 1
    fi
    if [[ ! -f "$skill_root/$skill/act_policy_step_120000.pth" ]]; then
        echo "Missing $skill_root/$skill/act_policy_step_120000.pth"
        exit 1
    fi
    if [[ ! -f "$skill_root/$skill/dataset_stats.pt" ]]; then
        echo "Missing $skill_root/$skill/dataset_stats.pt"
        exit 1
    fi
done

if [[ ! -f "$skill_root/pick-socks-feb3-sock-state/sock_yolo11n_best.pt" ]]; then
    echo "Missing $skill_root/pick-socks-feb3-sock-state/sock_yolo11n_best.pt"
    exit 1
fi

if [[ "$target" == *:* ]]; then
    remote="${target%%:*}"
    remote_path="${target#*:}"
    ssh "$remote" "mkdir -p '$remote_path/workspace/custom_skills'"
else
    mkdir -p "$target/workspace/custom_skills"
fi

rsync -ah --progress \
    "$skill_root/pick-socks-feb3-baseline" \
    "$skill_root/pick-socks-feb3-sock-state" \
    "$target/workspace/custom_skills/"
