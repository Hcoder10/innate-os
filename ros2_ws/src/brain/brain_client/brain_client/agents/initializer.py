#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Brain Client Initializers

This module contains initialization functions for skills and agents
to keep the main brain_client_node.py clean and focused.
"""

from brain_client.agents.loader import AgentLoader
from brain_client.agents.types import Agent
from brain_client.common.script_paths import (
    ensure_user_directories,
    get_agent_directories,
    get_workspace_dir,
)
from brain_client.skills.physical_refs import render_refs, write_refs


def initialize_agents(logger, skills_dict: dict[str, dict] | None = None) -> tuple[dict[str, Agent], Agent | None]:
    """
    Initialize all agents using dynamic loading.

    Args:
        logger: ROS logger instance
        skills_dict: Optional dictionary of available skills for validation

    Returns:
        Tuple of (agents_dict, default_agent) where:
        - agents_dict: Dictionary mapping agent names to their instances
        - default_agent: The default agent instance to use
    """
    agent_loader = AgentLoader(logger)

    # Ensure custom dirs exist. Agents are scanned from workspace/custom_agents
    # and, if present, ~/agents (in place — never moved).
    ensure_user_directories()

    # Agent files may `from physical_skills import X`, so make sure the
    # generated package exists before importing them (a fresh workspace where
    # agents load before the skills server has written it). The skills server
    # is the authoritative writer; this only fills the ordering gap.
    _regenerate_physical_refs(logger, skills_dict)

    agents_directories = [str(p) for p in get_agent_directories()]

    # Load all agents dynamically from all directories
    discovered_agent_classes = agent_loader.load_from_directories(agents_directories)

    # Create agent instances with skill validation and icon loading.
    # The loader stamps `source` per instance based on origin file path.
    agents = agent_loader.create_agent_instances(
        discovered_agent_classes,
        available_skills=skills_dict,
    )

    logger.info(f"Successfully loaded {len(agents)} agents")

    # Set default agent (fallback to first available if empty_directive not found)
    # Note: This doesn't mean the agent runs - is_brain_active controls that
    default_agent = None
    if "empty_directive" in agents:
        default_agent = agents["empty_directive"]
        logger.debug("Using empty_directive as default")
    elif agents:
        first_agent_name = next(iter(agents))
        default_agent = agents[first_agent_name]
        logger.debug(f"Using {first_agent_name} as default agent")
    else:
        logger.error("No agents loaded! This will cause issues.")

    return agents, default_agent


def _regenerate_physical_refs(logger, skills_dict: dict[str, dict] | None) -> None:
    """Write workspace/physical_skills/ from the roster metadata, only when
    the generated package doesn't exist yet. Skipped when no roster is
    available (nothing to generate from) or when the skills server has
    already written the package: the server regenerates it on every load and
    publish from its full pre-dedupe roster, while this roster has
    display-name dedupe applied — rewriting here from the (possibly smaller)
    deduped set would make the two processes overwrite each other's file
    forever, each write triggering the watcher's full reload."""
    if not skills_dict:
        return
    if (get_workspace_dir() / "physical_skills" / "__init__.py").exists():
        return
    # Everything on the roster that isn't a code skill is a physical skill
    # (learned/replay/eval/poses/...; broken entries never reach the registry).
    entries = [meta for meta in skills_dict.values() if meta.get("type") != "code"]
    write_refs(get_workspace_dir() / "physical_skills", render_refs(entries), logger)
