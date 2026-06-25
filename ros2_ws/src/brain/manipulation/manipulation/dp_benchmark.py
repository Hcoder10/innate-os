#!/usr/bin/env python3
"""Standalone latency benchmark for the on-robot Diffusion Policy (DP.py).

No ROS / no dataset needed — builds the policy (random weights unless a
checkpoint is given) and times the denoising replan on dummy observations. The
key number is the per-control-tick cost: one ``predict_action`` denoise produces
``n_action_steps`` actions, so its cost is amortized over that many 25 Hz ticks.
The 25 Hz control loop budget is 40 ms/tick.

Examples:
    python3 dp_benchmark.py                          # sweep steps on best device
    python3 dp_benchmark.py --steps 4,8,16,32        # latency vs DDIM steps
    python3 dp_benchmark.py --checkpoint run/dp_policy_step_120000.pth \
        --stats run/dataset_stats.pt --device cuda
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import DP  # noqa: E402  (self-contained, no ROS deps)

CONTROL_HZ = 25.0
BUDGET_MS = 1000.0 / CONTROL_HZ  # 40 ms


def _identity_stats(action_dim):
    z3, o3 = torch.zeros(3, 1, 1), torch.ones(3, 1, 1)
    return {
        "observation.image_camera_1": {"mean": z3, "std": o3},
        "observation.image_camera_2": {"mean": z3, "std": o3},
        "observation.state": {"mean": torch.zeros(6), "std": torch.ones(6)},
        "action": {"mean": torch.zeros(action_dim), "std": torch.ones(action_dim)},
    }


def build_policy(args, num_inference_steps):
    device = torch.device(args.device)
    if args.checkpoint:
        stats = torch.load(args.stats, map_location="cpu") if args.stats else _identity_stats(args.action_dim)
        policy, _ = DP.load_dp_policy(
            args.checkpoint, stats, action_dim=args.action_dim, device=device,
            horizon=args.horizon, n_action_steps=args.n_action_steps,
            num_inference_steps=num_inference_steps,
        )
    else:
        cfg = DP.DPRuntimeConfig(
            input_shapes={
                "observation.image_camera_1": [3, 224, 224],
                "observation.image_camera_2": [3, 224, 224],
                "observation.state": [6],
            },
            output_shapes={"action": [args.action_dim]},
            horizon=args.horizon, n_action_steps=args.n_action_steps,
            num_inference_steps=num_inference_steps,
        )
        policy = DP.DiffusionPolicy(cfg, dataset_stats=_identity_stats(args.action_dim)).to(device).eval()
    return policy, device


def dummy_obs(device):
    return {
        "observation.image_camera_1": torch.rand(1, 3, 224, 224, device=device),
        "observation.image_camera_2": torch.rand(1, 3, 224, 224, device=device),
        "observation.state": torch.randn(1, 6, device=device),
    }


def time_calls(policy, device, n, warmup):
    obs = dummy_obs(device)
    cuda = device.type == "cuda"
    for _ in range(warmup):
        policy.predict_action(obs)
    if cuda:
        torch.cuda.synchronize()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        policy.predict_action(obs)
        if cuda:
            torch.cuda.synchronize()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return samples


def pct(s, p):
    return s[min(len(s) - 1, int(p * len(s)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="4,8,16,32", help="comma list of DDIM inference steps to sweep")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=16)
    ap.add_argument("--n-action-steps", type=int, default=8)
    ap.add_argument("--action-dim", type=int, default=10)
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--stats", default=None)
    args = ap.parse_args()

    steps_list = [int(s) for s in args.steps.split(",")]
    print(f"device={args.device}  horizon={args.horizon}  n_action_steps={args.n_action_steps}  "
          f"control budget={BUDGET_MS:.0f} ms/tick @ {CONTROL_HZ:.0f} Hz\n")
    print(f"{'steps':>6} {'plan_med_ms':>12} {'plan_p90_ms':>12} {'per_tick_ms':>12} {'max_hz':>8} {'fits40ms':>9}")
    print("-" * 64)
    for steps in steps_list:
        policy, device = build_policy(args, steps)
        s = time_calls(policy, device, args.iters, args.warmup)
        med, p90 = pct(s, 0.5), pct(s, 0.9)
        per_tick = med / max(1, args.n_action_steps)  # amortized over the receding horizon
        max_hz = 1000.0 / per_tick
        fits = "yes" if per_tick <= BUDGET_MS else "NO"
        print(f"{steps:>6} {med:>12.1f} {p90:>12.1f} {per_tick:>12.2f} {max_hz:>8.0f} {fits:>9}")
    print("\nNote: per_tick_ms = plan_med_ms / n_action_steps (one denoise feeds the action queue).")
    print("      per_tick is the AMORTIZED cost and only holds if replanning runs asynchronously;")
    print("      synchronously (current select_action), plan_med_ms is a hard stall every n_action_steps ticks.")
    print("This is the torch-eager reference (no TensorRT); a TRT engine at DP._unet_forward is the path to budget.")


if __name__ == "__main__":
    main()
