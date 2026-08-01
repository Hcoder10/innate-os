#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Brain Client Initializers

This module contains initialization functions for skills and agents
to keep the main brain_client_node.py clean and focused.
"""

from brain_client.agents.loader import build_agent_instances, discover_agent_classes
from brain_client.agents.types import Agent
from brain_client.common.script_paths import ensure_user_directories, get_workspace_dir
from brain_client.skills.physical_refs import render_dir_shims, render_refs, write_dir_shims, write_refs


def initialize_agents(
    logger, skills_dict: dict[str, dict] | None = None
) -> tuple[dict[str, Agent], Agent | None, dict[str, str]]:
    """
    Initialize all agents by importing the agent packages.

    Args:
        logger: ROS logger instance
        skills_dict: Optional dictionary of available skills for validation

    Returns:
        Tuple of (agents_dict, default_agent, broken) where:
        - agents_dict: Dictionary mapping agent ids to their instances
        - default_agent: The default agent instance to use
        - broken: name -> load-error text for agents that failed to load,
          published on get_available_directives so they stay visible in the
          UI with their error instead of silently vanishing
    """
    # Ensure custom dirs exist before importing.
    ensure_user_directories()

    # Agent files may `from physical_skills import X`, so make sure the
    # generated package exists before importing them (a fresh workspace where
    # agents load before the skills server has written it). The skills server
    # is the authoritative writer; this only fills the ordering gap.
    _regenerate_physical_refs(logger, skills_dict)

    classes, import_errors = discover_agent_classes(logger)
    agents, broken = build_agent_instances(classes, logger, available_skills=skills_dict)
    broken = {**import_errors, **broken}

    logger.info(f"Successfully loaded {len(agents)} agents")
    if broken:
        logger.warning(f"{len(broken)} agents failed to load: {list(broken)}")

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

    return agents, default_agent, broken


def _regenerate_physical_refs(logger, skills_dict: dict[str, dict] | None) -> None:
    """Write workspace/physical_skills/ from the roster metadata, only when
    the generated package doesn't exist yet. Skipped when no roster is
    available (nothing to generate from) or when the skills server has
    already written the package: the server regenerates it on every load and
    publish from its full pre-dedupe roster, while this roster has
    display-name dedupe applied — rewriting here from the (possibly smaller)
    deduped set would make the two processes overwrite each other's file
    forever, each write triggering the watcher's full reload.

    The per-recording-folder ``__init__.py`` shims are written under the same
    guard, for the same reason the package is: agents also spell refs as
    ``from innate_skills.<x> import <X>``, and importing a recording folder
    before its shim exists caches it as an *empty namespace package* —
    a state a reload can only undo because ``evict_modules_under`` evicts
    namespace packages by ``__path__``. Since the shims stopped being
    committed (they made ``git pull`` abort on robots whose running code was
    ahead of their checkout), the runtime is their only writer, so this
    ordering gap is real on any fresh workspace. No prune_dir_shims sweep
    here: the server owns cleanup of folders that left the roster."""
    if not skills_dict:
        return
    if (get_workspace_dir() / "physical_skills" / "__init__.py").exists():
        return
    # Everything on the roster that isn't a code skill is a physical skill
    # (learned/replay/eval/poses/...; broken entries never reach the registry).
    # The roster's `directory` is the recording folder, keyed as `dir` for
    # render_dir_shims — same mapping the catalog does in _write_physical_refs.
    entries = [{**meta, "dir": meta.get("directory")} for meta in skills_dict.values() if meta.get("type") != "code"]
    write_refs(get_workspace_dir() / "physical_skills", render_refs(entries), logger)
    write_dir_shims(render_dir_shims(entries), logger)
