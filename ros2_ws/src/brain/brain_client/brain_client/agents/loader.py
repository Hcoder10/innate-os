# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Import-based agent discovery: agents register by existing, same as skills.

``workspace/innate_agents`` and ``workspace/custom_agents`` are ordinary
Python packages, imported with the same machinery skill packages use
(``skills/workspace_import.py``): every module is imported, defining an
``Agent`` subclass registers it (``Agent.__init_subclass__``), and a module
that raises is reported broken with its real error instead of silently
vanishing from the roster. Because these are real imports, ordinary Python
works in agent packages too — helper modules next to an agent, folder agents,
several agents per file.
"""

from __future__ import annotations

import base64
import inspect
import sys
from pathlib import Path

from brain_client.agents.types import Agent
from brain_client.common.dynamic_loader import class_name_to_snake_case, evict_modules_under
from brain_client.common.script_paths import (
    classify_source,
    get_custom_agents_dir,
    get_innate_agents_dir,
    get_workspace_dir,
)
from brain_client.skills.workspace_import import format_load_error, import_packages, live_class


def discover_agent_classes(logger) -> tuple[list[tuple[type[Agent], Path]], dict[str, str]]:
    """Re-import the agent packages; returns ``(classes, import_errors)``.

    ``classes`` is every live registered Agent class with its source file,
    ordered innate before custom so a custom agent wins an id conflict (same
    precedence the directory scan had) — sorted explicitly, because registry
    order is dict insertion order, and a dict re-insert keeps a key's original
    slot while a genuinely new key (an innate module that first imports after
    boot, or a renamed class) appends at the end, after the custom entries.
    Includes abstract classes, which surface as broken entries from
    ``build_agent_instances``.
    ``import_errors`` maps module name -> error text for modules that failed
    to import; the caller rosters these as broken so the UI can show them.

    The whole workspace subtree is evicted first — the same reload model as
    the skill catalog: agent modules import skill classes and the generated
    ``physical_skills`` refs, so re-importing agents over stale cached copies
    would pin whatever those modules held at the previous load.
    """
    evict_modules_under([str(get_workspace_dir())])
    errors = import_packages([get_innate_agents_dir(), get_custom_agents_dir()], logger)

    classes: list[tuple[type[Agent], Path]] = []
    for (module_name, qualname), cls in list(Agent._registry.items()):
        module = sys.modules.get(module_name)
        bound = live_class(module, qualname) if module is not None else None
        if bound is not cls:
            # Stale registration: module evicted, or re-imported without this
            # class. Same pruning as registered_workspace_skills — including
            # the warning for the one live case (a function-local Agent) where
            # silent pruning would be undiagnosable for the author.
            if module is not None and "<locals>" in qualname:
                logger.warning(
                    f"Agent {qualname} in {module_name} is defined inside a function and cannot be "
                    "loaded; define it at module level."
                )
            del Agent._registry[(module_name, qualname)]
            continue
        if module_name.startswith("workspace."):
            continue  # compat-path double import; the bare-name module owns the registration
        if cls.__name__.startswith("_"):
            continue  # helper base by convention, deliberately not an agent
        if inspect.isabstract(cls):
            # Not skipped: it falls through to build_agent_instances, where
            # cls() raises and rosters it broken. Helper bases use `_` above.
            missing = ", ".join(sorted(getattr(cls, "__abstractmethods__", ())))
            logger.warning(
                f"Agent {cls.__name__} in {module_name} is abstract (unimplemented: {missing}); "
                "implement the missing members, or prefix the class with '_' if it is a helper base."
            )
        source_file = getattr(module, "__file__", None)
        if source_file is None:
            continue
        classes.append((cls, Path(source_file)))
    # Stable: innate first, custom last, registration order within each.
    classes.sort(key=lambda entry: entry[0].__module__.partition(".")[0] != "innate_agents")
    return classes, errors


def build_agent_instances(
    classes: list[tuple[type[Agent], Path]],
    logger,
    available_skills: dict[str, dict] | None = None,
) -> tuple[dict[str, Agent], dict[str, str]]:
    """Instantiate discovered agent classes; returns ``(agents by id, broken)``.

    Construction probes every surface the brain reads later (id, display
    name, prompt, skill and input refs, icon) so a bad agent fails here — as
    a broken roster entry with its error — rather than inside the directives
    service or at activation, where one bad agent would take the whole
    response down.
    """
    agents: dict[str, Agent] = {}
    broken: dict[str, str] = {}
    for cls, source_file in classes:
        try:
            agent = cls()
            agent_id = agent.id
            agent.source = classify_source(source_file)
            _load_display_icon(agent, source_file.parent, logger)
            str(agent.display_name)
            agent.get_prompt()
            agent.input_names()
            skill_ids = agent.skill_ids()
        except Exception as e:  # noqa: BLE001 — one bad agent must not stop the roster
            name = class_name_to_snake_case(cls.__name__)
            if name in broken:  # same class name broken more than once — keep every row
                base = f"{cls.__module__}.{name}"
                name, n = base, 2
                while name in broken:  # same name twice in one module (nested classes)
                    name, n = f"{base}.{n}", n + 1
            broken[name] = format_load_error(e)
            logger.error(f"Error loading agent {cls.__name__} from {source_file}: {broken[name]}")
            continue
        if available_skills is not None:
            missing_skills = [skill_id for skill_id in skill_ids if skill_id not in available_skills]
            if missing_skills:
                logger.warning(f"Agent '{agent_id}' references skills that are not available: {missing_skills}")
        if agent_id in agents:
            logger.warning(
                f"Agent id conflict: '{agent_id}' defined by both "
                f"{type(agents[agent_id]).__module__} and {cls.__module__}. Using the latter."
            )
        agents[agent_id] = agent
        logger.debug(f"Created agent instance: {agent_id} (source={agent.source})")
    return agents, broken


def _load_display_icon(agent: Agent, directory: Path, logger) -> None:
    """Base64-encode the agent's display icon, resolved relative to its own
    source directory (works for folder agents too)."""
    if not agent.display_icon:
        return
    icon_path = directory / agent.display_icon
    if icon_path.exists():
        try:
            agent.display_icon_data = base64.b64encode(icon_path.read_bytes()).decode("utf-8")
        except Exception as e:  # noqa: BLE001 — an unreadable icon must not break the agent
            logger.warning(f"Failed to load icon for agent '{agent.id}': {e}")
