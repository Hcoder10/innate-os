"""Tunable-parameter registry: the single source of truth for user-facing config.

This module is intentionally ROS-free so it can be imported by the `innate
config` CLI and, later, the web app. It maps friendly knob names
(e.g. ``nav.max_speed``) to the underlying ROS node + parameter, validates
values against the registry, and reads/writes the central ``overrides.yaml``
that the launch files load.
"""

import os
from pathlib import Path

import yaml

_REGISTRY_REL = "ros2_ws/src/innate_config/config/registry.yaml"
_OVERRIDES_REL = "ros2_ws/src/innate_config/config/overrides.yaml"

_OVERRIDES_HEADER = """\
# ============================================================================
#  Innate OS — active parameter overrides   (managed by `innate config`)
# ============================================================================
#  Prefer the CLI:  `innate config`  (list)  ·  `innate config set <name> <v>`
#  It validates values and hides ROS node names. Hand-edits are allowed but
#  the CLI rewrites this file, so comments here are not preserved.
#  Browse everything tunable + descriptions with `innate config`.
# ============================================================================
"""

_BASELINE_NODE = "/**"


class ConfigError(Exception):
    """Raised for unknown knobs or invalid values (carries a user-facing message)."""


def innate_os_root() -> Path:
    return Path(os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")))


def registry_path() -> Path:
    return innate_os_root() / _REGISTRY_REL


def overrides_path() -> Path:
    return innate_os_root() / _OVERRIDES_REL


# --------------------------------------------------------------------------- #
# Registry loading
# --------------------------------------------------------------------------- #
def load_registry() -> list:
    """Return the list of knob definitions, ordered as written in the file."""
    with open(registry_path()) as f:
        entries = yaml.safe_load(f) or []
    return entries


def registry_by_name() -> dict:
    return {e["name"]: e for e in load_registry()}


def entry(name: str) -> dict:
    try:
        return registry_by_name()[name]
    except KeyError:
        raise ConfigError(f"Unknown parameter '{name}'. Run `innate config` to list them.")


# --------------------------------------------------------------------------- #
# Value validation / coercion
# --------------------------------------------------------------------------- #
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def coerce(e: dict, raw) -> object:
    """Validate ``raw`` against the entry's type/bounds and return a typed value."""
    t = e["type"]
    name = e["name"]

    if t == "bool":
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ConfigError(f"{name}: expected true/false, got '{raw}'")

    if t in ("int", "float"):
        try:
            value = int(raw) if t == "int" else float(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}: expected {t}, got '{raw}'")
        lo, hi = e.get("min"), e.get("max")
        if lo is not None and value < lo:
            raise ConfigError(f"{name}: {value} is below minimum {lo}")
        if hi is not None and value > hi:
            raise ConfigError(f"{name}: {value} is above maximum {hi}")
        return value

    if t == "enum":
        choices = e.get("choices", [])
        for c in choices:
            if str(raw) == str(c):
                return c
        raise ConfigError(f"{name}: must be one of {choices}, got '{raw}'")

    # str
    return str(raw)


# --------------------------------------------------------------------------- #
# Overrides file read / write
# --------------------------------------------------------------------------- #
def _read_overrides() -> dict:
    path = overrides_path()
    if not path.is_file():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _write_overrides(data: dict) -> None:
    """Write the overrides file: header + `/**` baseline + active node sections.

    Nodes whose ``ros__parameters`` ended up empty are dropped (an empty
    ``ros__parameters:`` is a ROS parse error); the ``/**`` baseline keeps the
    file a valid, non-empty parameter source.
    """
    out = {_BASELINE_NODE: {"ros__parameters": {}}}
    for node, body in data.items():
        if node == _BASELINE_NODE:
            continue
        params = (body or {}).get("ros__parameters") or {}
        if params:
            out[node] = {"ros__parameters": params}

    with open(overrides_path(), "w") as f:
        f.write(_OVERRIDES_HEADER)
        yaml.safe_dump(out, f, default_flow_style=False, sort_keys=True)


def _node_params(data: dict, node: str) -> dict:
    return data.setdefault(node, {}).setdefault("ros__parameters", {})


def _set_nested(params: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    d = params
    for k in keys[:-1]:
        d = d.setdefault(k, {})
        if not isinstance(d, dict):  # a scalar was sitting where we need a map
            raise ConfigError(f"Cannot set '{dotted}': '{k}' is not a group")
    d[keys[-1]] = value


def _get_nested(params: dict, dotted: str):
    d = params
    for k in dotted.split("."):
        if not isinstance(d, dict) or k not in d:
            return (False, None)
        d = d[k]
    return (True, d)


def _unset_nested(params: dict, dotted: str) -> None:
    keys = dotted.split(".")
    stack = [params]
    for k in keys[:-1]:
        if not isinstance(stack[-1], dict) or k not in stack[-1]:
            return
        stack.append(stack[-1][k])
    stack[-1].pop(keys[-1], None)
    # prune now-empty parent groups
    for k, parent in zip(reversed(keys[:-1]), reversed(stack[:-1])):
        child = parent.get(k)
        if isinstance(child, dict) and not child:
            parent.pop(k, None)


# --------------------------------------------------------------------------- #
# Public read/write API (by friendly name)
# --------------------------------------------------------------------------- #
def get_override(name: str):
    """Return ``(is_set, value)`` for the current override of ``name``."""
    e = entry(name)
    data = _read_overrides()
    body = data.get(e["node"], {})
    params = body.get("ros__parameters", {}) if isinstance(body, dict) else {}
    return _get_nested(params, e["param"])


def current_value(name: str):
    is_set, value = get_override(name)
    return value if is_set else entry(name).get("default")


def set_value(name: str, raw) -> object:
    """Validate and persist an override. Returns the typed value written."""
    e = entry(name)
    value = coerce(e, raw)
    data = _read_overrides()
    _set_nested(_node_params(data, e["node"]), e["param"], value)
    _write_overrides(data)
    return value


def unset_value(name: str) -> None:
    """Remove an override so the knob falls back to its package default."""
    e = entry(name)
    data = _read_overrides()
    body = data.get(e["node"])
    if isinstance(body, dict):
        _unset_nested(body.get("ros__parameters", {}), e["param"])
    _write_overrides(data)
