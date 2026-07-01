#!/usr/bin/env python3
"""Standalone DP weight load + inference check. No ROS, no robot.

Loads your checkpoint into DP.py, reports how many keys actually matched, then
runs one real denoise on dummy obs and prints the action tensor.
"""
import argparse, os, sys
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import DP


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--stats", default=None, help="dataset_stats.pt (recommended)")
    ap.add_argument("--action-dim", type=int, default=10)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    device = torch.device(args.device)
    stats = torch.load(args.stats, map_location="cpu") if args.stats else None

    policy, result = DP.load_dp_policy(
        args.checkpoint, stats, action_dim=args.action_dim, device=device,
    )

    total = sum(1 for _ in policy.state_dict())
    print(f"\n=== weight load ===")
    print(f"model tensors:       {total}")
    print(f"missing keys:        {len(result.missing_keys)}")
    print(f"unexpected keys:     {len(result.unexpected_keys)}")
    if result.missing_keys:
        print(f"  e.g. missing:      {result.missing_keys[:5]}")
    if result.unexpected_keys:
        print(f"  e.g. unexpected:   {result.unexpected_keys[:5]}")
    ok = not result.missing_keys and not result.unexpected_keys
    print(f"clean match:         {'YES' if ok else 'NO — architecture/shape mismatch'}")

    obs = {
        "observation.image_camera_1": torch.rand(1, 3, 224, 224, device=device),
        "observation.image_camera_2": torch.rand(1, 3, 224, 224, device=device),
        "observation.state": torch.randn(1, 6, device=device),
    }
    with torch.no_grad():
        actions = policy.predict_action(obs)
    print(f"\n=== inference ===")
    print(f"action tensor shape: {tuple(actions.shape)}  (B, n_action_steps, action_dim)")
    print(f"finite:              {bool(torch.isfinite(actions).all())}")
    print(f"range:               [{actions.min():.3f}, {actions.max():.3f}]")
    print(f"first action:        {actions[0, 0].cpu().numpy().round(3)}\n")


if __name__ == "__main__":
    main()
