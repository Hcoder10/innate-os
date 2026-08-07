# `core/` — the brain's behaviour

The orchestration layer. Knows the perception/skills/transport collaborators and
coordinates them; it does **not** own ROS transport details (those are in the
concept folders) and should stay free of `rclpy` message plumbing where possible.

- `lifecycle.py` — activate / deactivate / reset state machine and directive
  switching. Starts/stops the agent's loop task (`brain/agent.py`) and the
  on-demand sensor subscriptions.
- `config.py` — **pure**: ROS params → frozen `BrainConfig` dataclass.
- `state.py` — the shared, named, mutable cross-cutting flags + registry/directive.
