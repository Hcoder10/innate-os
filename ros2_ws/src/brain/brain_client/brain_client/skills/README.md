# `skills/` — the skill system

- `registry.py` — **pure**: the single source of truth for skill name ↔ id mapping
  and `resolve_skill_id` (replaces three inconsistent inline translations). Used by
  the brain client to translate the cloud agent's task types.
- `runner.py` — brain-client side: the `execute_skill` action *client* lifecycle
  (send goal, handle result/cancel/feedback, pending-task-behind-a-cancellation).
  The "activate a primitive" sequence lives once here as `start_task`.
- `catalog.py` — skills-server side: skill discovery, input introspection,
  `AvailableSkills` publishing, full/selective reload, physical-skill loading +
  creation, and the skill cache. Owns the loaded skill dicts (thread-safe getters).
- `robot_state.py` — skills-server side: on-demand state subscriptions, interface
  injection, and the 50 Hz live robot-state update thread for the running skill.
- `registration.py` — tracks `/brain/available_skills` and registers skills +
  directive with the cloud agent (with re-registration deferral while busy).
- `hot_reload.py` — full/selective reload coordination and the file-watcher queue.
- `loader.py` / `cli_bridge.py` / `hot_reload_watcher.py` — skill loading + CLI
  bridge + the filesystem watcher.
- `types.py` — the public `Skill` / `SkillResult` SDK base classes that user skills
  in `workspace/` import. Keep this import path stable.
