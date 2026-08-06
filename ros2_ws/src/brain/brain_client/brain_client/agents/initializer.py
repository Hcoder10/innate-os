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
from brain_client.skills.registry import SkillMeta
from brain_client.skills.workspace_import import unique_key

# module name -> agent ids it registered on its last clean import, carried
# across reloads: a module that later fails to import rosters every agent it
# used to define as broken, not one module-derived row that would collapse a
# multi-agent file to a single entry (same contract as the skill catalog's
# _skill_ids_by_module). Process-lifetime, like Agent._registry.
_agent_ids_by_module: dict[str, list[str]] = {}


def initialize_agents(
    logger, skills_dict: dict[str, SkillMeta] | None = None
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
    agents, probe_errors = build_agent_instances(classes, logger, available_skills=skills_dict)
    # Module rows keyed like class rows (innate_agents.foo -> foo) so a broken
    # entry keeps its name when a syntax error becomes a class-level failure;
    # full module name if that key is taken. All merges go through unique_key
    # so no row (class over module, probe over import) can shadow another.
    ids_by_module: dict[str, list[str]] = {}
    for agent_id, agent in agents.items():
        ids_by_module.setdefault(type(agent).__module__, []).append(agent_id)
    # A probe failure must not erase module identity: its class imported (it's
    # in `classes`) but built no instance, so the live roster alone would drop
    # the module from the carryover — and a later *import* failure would then
    # collapse the module to one row instead of its known agent ids. Keep the
    # ids from the last build that had them. A module whose classes all build
    # never merges, so a genuinely removed agent still ages out.
    built = {type(agent) for agent in agents.values()}
    for cls, _source in classes:
        prior = _agent_ids_by_module.get(cls.__module__)
        if cls in built or not prior:
            continue
        ids = ids_by_module.setdefault(cls.__module__, [])
        ids.extend(agent_id for agent_id in prior if agent_id not in ids)
    broken: dict[str, str] = {}
    for module_name, error in import_errors.items():
        known_ids = _agent_ids_by_module.get(module_name)
        if known_ids:
            # The module imported cleanly earlier in this process, so we know
            # which agents live in it: roster each one broken rather than one
            # module row — a multi-agent file must not collapse to a single
            # entry, vanishing every agent but one. The module-derived row is
            # only for modules that never imported cleanly in this process.
            ids_by_module[module_name] = known_ids  # carry through the breakage
            for agent_id in known_ids:
                broken[unique_key(broken, agent_id)] = error
            continue
        name = module_name.partition(".")[2] or module_name
        if name in broken:
            name = module_name
        broken[unique_key(broken, name)] = error
    _agent_ids_by_module.clear()
    _agent_ids_by_module.update(ids_by_module)
    for name, error in probe_errors.items():
        broken[unique_key(broken, name)] = error

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


def _regenerate_physical_refs(logger, skills_dict: dict[str, SkillMeta] | None) -> None:
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
