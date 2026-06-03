# `core/` — the brain's behaviour

The orchestration layer. Knows the perception/skills/transport collaborators and
coordinates them; it does **not** own ROS transport details (those are in the
concept folders) and should stay free of `rclpy` message plumbing where possible.

- `orchestrator.py` — the perception loop: gather image + pose + map, build the
  payload, send it; plus reactions to the cloud agent's control messages.
- `lifecycle.py` — activate / deactivate / reset / reactivate state machine and
  directive switching. Owns the agent timer and on-demand sensor subscriptions.
- `vision_output.py` — react to the agent's `next_task`: chat + drive the runner.
- `config.py` — **pure**: ROS params → frozen `BrainConfig` dataclass.
- `state.py` — the shared, named, mutable cross-cutting flags + registry/directive.
