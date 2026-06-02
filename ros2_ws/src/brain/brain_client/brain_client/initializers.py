#!/usr/bin/env python3
"""
Brain Client Initializers

This module contains initialization functions for skills and agents
to keep the main brain_client_node.py clean and focused.
"""

from typing import Dict, Any, Optional, Tuple

from brain_client.agent_loader import AgentLoader
from brain_client.script_paths import (
    ensure_user_directories,
    get_agent_directories,
)


def initialize_agents(
    logger, skills_dict: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Optional[Any]]:
    """
    Initialize all agents using dynamic loading.

    Args:
        logger: ROS logger instance
        skills_dict: Optional dictionary of available skills for validation

    Returns:
        Tuple of (agents_dict, current_agent) where:
        - agents_dict: Dictionary mapping agent names to their instances
        - current_agent: None until the user selects an agent
    """
    agent_loader = AgentLoader(logger)

    # Ensure custom dirs exist. Agents are scanned from workspace/custom_agents
    # and, if present, ~/agents (in place — never moved).
    ensure_user_directories()

    agents_directories = [str(p) for p in get_agent_directories()]

    # Load all agents dynamically from all directories
    discovered_agent_classes = agent_loader.load_agents_from_directories(
        agents_directories
    )

    # Create agent instances with skill validation and icon loading.
    # The loader stamps `source` per instance based on origin file path.
    agents = agent_loader.create_agent_instances(
        discovered_agent_classes,
        available_skills=skills_dict,
    )

    logger.info(f"Successfully loaded {len(agents)} agents")

    if agents:
        logger.info("No default agent selected; waiting for /brain/set_directive")
    else:
        logger.error("No agents loaded! This will cause issues.")

    return agents, None
