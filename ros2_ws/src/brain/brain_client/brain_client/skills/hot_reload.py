"""Reload coordination: full reload, selective reload, and the hot-reload queue.

Drives PEAS (the skill action server) to reload skill code, reloads agents
locally, and re-registers with the cloud agent. The file watcher runs on a
background thread and only *queues* work; the actual reload runs on the ROS
executor thread via :meth:`process_queue` (a node timer callback).
"""

from __future__ import annotations

import threading
import time

import rclpy
from brain_messages.srv import ReloadSkillsAgents
from std_srvs.srv import Trigger

from brain_client.agents.initializer import initialize_agents
from brain_client.common.ros_services import call_service_sync
from brain_client.common.script_paths import get_agent_directories
from brain_client.skills.hot_reload_watcher import HotReloadWatcher
from brain_client.skills.registry import SkillRegistry


class ReloadCoordinator:
    def __init__(
        self, node, state, lifecycle, catalog, service_call_node, reload_primitives_client, reload_skills_client
    ):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._lifecycle = lifecycle
        self._catalog = catalog
        self._service_call_node = service_call_node
        self._reload_primitives_client = reload_primitives_client
        self._reload_skills_client = reload_skills_client

        self._pending = None  # (skill_names, agent_names)
        self._lock = threading.Lock()
        self._watcher = None
        self._timer = None

    # --- watcher lifecycle ---
    def start_watcher(self) -> None:
        self._watcher = HotReloadWatcher(
            logger=self._logger,
            skills_directories=[],  # skill hot reload is handled by PEAS/SAS
            agents_directories=[str(p) for p in get_agent_directories()],
            on_reload=self.queue,
            debounce_seconds=1.0,
        )
        self._watcher.start()
        self._timer = self._node.create_timer(0.5, self.process_queue)

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    # --- queue (watchdog thread -> executor thread) ---
    def queue(self, skill_names: list, agent_names: list) -> None:
        with self._lock:
            if self._pending is not None:
                existing_skills, existing_agents = self._pending
                self._pending = (
                    list(set(existing_skills + skill_names)),
                    list(set(existing_agents + agent_names)),
                )
            else:
                self._pending = (list(skill_names), list(agent_names))
        self._logger.info(f"Queued hot reload: skills={skill_names}, agents={agent_names}")

    def process_queue(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return
        skill_names, agent_names = pending
        self._logger.info(f"Processing hot reload: skills={skill_names}, agents={agent_names}")
        try:
            self.perform_selective(skill_names, agent_names)
        except Exception as e:
            self._logger.error(f"Hot reload failed: {e}")

    # --- reload operations ---
    def perform_selective(self, skill_names: list, agent_names: list) -> tuple:
        reloaded_skills: list = []
        reloaded_agents: list = []
        try:
            self._lifecycle.deactivate_brain()

            if skill_names:
                req = ReloadSkillsAgents.Request()
                req.skills = skill_names
                req.agents = []  # PEAS doesn't handle agents
                result = call_service_sync(
                    self._service_call_node,
                    self._logger,
                    self._reload_skills_client,
                    req,
                    "PEAS selective reload",
                    timeout_sec=15.0,
                )
                if result and result.success:
                    reloaded_skills = list(result.reloaded_skills)

            if agent_names:
                reloaded_agents = self._reload_agents(agent_names)

            if reloaded_skills or reloaded_agents:
                self._catalog.register()

            self._logger.info(
                f"[BrainClient] Selective reload complete: {len(reloaded_skills)} skills, {len(reloaded_agents)} agents"
            )
        except Exception as e:
            self._logger.error(f"[BrainClient] Selective reload failed: {e}")
            raise
        return reloaded_skills, reloaded_agents

    def perform_full(self) -> None:
        try:
            self._lifecycle.deactivate_brain()
            self._state.directives = {}
            self._state.registry = SkillRegistry()
            self._state.current_directive = None

            call_service_sync(
                self._service_call_node,
                self._logger,
                self._reload_primitives_client,
                Trigger.Request(),
                "PEAS reload",
                timeout_sec=30.0,
                wait_timeout_sec=10.0,
            )

            deadline = time.time() + 5.0
            while not self._state.registry and time.time() < deadline:
                rclpy.spin_once(self._node, timeout_sec=0.2)
            if not self._state.registry:
                self._logger.warn("No primitives received from topic after reload")

            self._state.directives, self._state.current_directive = initialize_agents(
                self._logger, self._state.registry.primitives
            )
            self._catalog.register()
            self._logger.info(
                f"[BrainClient] Reloaded {len(self._state.registry.primitives)} primitives, "
                f"{len(self._state.directives)} directives"
            )
        except Exception as e:
            self._logger.error(f"[BrainClient] Reload failed: {e}")
            try:
                self._lifecycle.reactivate_brain()
            except Exception:
                pass

    def _reload_agents(self, agent_names: list) -> list:
        from brain_client.agents.loader import AgentLoader
        from brain_client.common.script_paths import classify_source

        agent_loader = AgentLoader(self._logger)
        agents_directories = [str(p) for p in get_agent_directories()]

        reloaded = []
        for agent_name in agent_names:
            entry = agent_loader.reload_agent_by_name(agent_name, agents_directories)
            if entry is None:
                continue
            agent_class, source_file = entry
            try:
                agent_instance = agent_class()
                agent_instance.source = classify_source(source_file)
                agent_loader._load_display_icon(agent_instance, str(source_file.parent))
                if self._state.registry:
                    agent_loader._validate_agent_skills(agent_instance, self._state.registry.primitives)
                self._state.directives[agent_name] = agent_instance
                reloaded.append(agent_name)
                if self._state.current_directive and self._state.current_directive.id == agent_name:
                    self._state.current_directive = agent_instance
                    self._logger.info(f"Updated current directive: {agent_name}")
                self._logger.info(f"Reloaded agent: {agent_name} [source={agent_instance.source}]")
            except Exception as e:
                self._logger.error(f"Error instantiating agent {agent_name}: {e}")
        return reloaded
