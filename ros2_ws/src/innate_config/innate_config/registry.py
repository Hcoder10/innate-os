"""Tunable-parameter registry: maps friendly knob names to ROS nodes/params.

ROS-free on purpose so it can be imported by the launch system today and a web
app later. Users tune the robot by hand-editing the friendly ``overrides.yaml``
(``nav.max_speed: 0.3``); at launch :func:`build_node_params` translates those
friendly names into the node-keyed ROS parameter structure the launch files
load. :func:`render_template` regenerates the self-documenting overrides file
from the registry.
"""

import yaml

from innate_config.paths import overrides_path, registry_path

_BASELINE_NODE = "/**"


class ConfigError(Exception):
    """Raised for unknown knobs or invalid values (carries a user-facing message)."""


# --------------------------------------------------------------------------- #
# Registry loading
# --------------------------------------------------------------------------- #
def load_registry() -> list:
    """Return the list of knob definitions, ordered as written in the file."""
    with open(registry_path()) as f:
        return yaml.safe_load(f) or []


def registry_by_name() -> dict:
    return {e["name"]: e for e in load_registry()}


def entry(name: str) -> dict:
    try:
        return registry_by_name()[name]
    except KeyError:
        raise ConfigError(f"Unknown parameter '{name}'. See overrides.yaml for the full list.")


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
        for c in e.get("choices", []):
            if str(raw) == str(c):
                return c
        raise ConfigError(f"{name}: must be one of {e.get('choices', [])}, got '{raw}'")

    # str
    return str(raw)


# --------------------------------------------------------------------------- #
# Friendly overrides file -> node-keyed ROS params
# --------------------------------------------------------------------------- #
def _set_nested(params: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    d = params
    for k in keys[:-1]:
        d = d.setdefault(k, {})
        if not isinstance(d, dict):  # a scalar was sitting where we need a map
            raise ConfigError(f"Cannot set '{dotted}': '{k}' is not a group")
    d[keys[-1]] = value


def load_overrides() -> dict:
    """Parse the friendly overrides file into ``{friendly_name: raw_value}``.

    Commented lines are ignored by the YAML parser, so only uncommented
    ``name: value`` entries are returned.
    """
    path = overrides_path()
    if not path.is_file():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def build_node_params():
    """Translate the friendly overrides into node-keyed ROS parameters.

    Returns ``(node_params, warnings)`` where ``node_params`` is
    ``{node: {"ros__parameters": {...}}}`` — always including a ``/**`` baseline
    so the file stays a valid, non-empty parameter source — and ``warnings`` is a
    list of human-readable strings for entries that were skipped (unknown name or
    a value that failed validation). Skipping keeps a single bad line from
    blocking the whole robot launch.
    """
    by_name = registry_by_name()
    out = {_BASELINE_NODE: {"ros__parameters": {}}}
    warnings = []
    for name, raw in load_overrides().items():
        e = by_name.get(name)
        if e is None:
            warnings.append(f"unknown parameter '{name}' — ignored")
            continue
        try:
            value = coerce(e, raw)
        except ConfigError as exc:
            warnings.append(f"{exc} — ignored, using default")
            continue
        node_params = out.setdefault(e["node"], {"ros__parameters": {}})["ros__parameters"]
        _set_nested(node_params, e["param"], value)
    return out, warnings


# --------------------------------------------------------------------------- #
# Friendly-name value lookups (for non-ROS consumers: sim launcher, web app)
# --------------------------------------------------------------------------- #
def overridden_values() -> dict:
    """Return ``{friendly_name: typed_value}`` for every knob set in overrides.yaml.

    Unknown or invalid entries are skipped. Knobs left at their default are not
    included — this mirrors "override-only" semantics for callers that should
    fall back to a downstream default when nothing is set.
    """
    by_name = registry_by_name()
    out = {}
    for name, raw in load_overrides().items():
        e = by_name.get(name)
        if e is None:
            continue
        try:
            out[name] = coerce(e, raw)
        except ConfigError:
            continue
    return out


def current_value(name: str):
    """Effective value of ``name``: its override if set and valid, else the default."""
    e = entry(name)
    raw = load_overrides().get(name)
    if raw is None:
        return e.get("default")
    try:
        return coerce(e, raw)
    except ConfigError:
        return e.get("default")


# --------------------------------------------------------------------------- #
# Self-documenting overrides file rendering
# --------------------------------------------------------------------------- #
_TEMPLATE_HEADER = """\
# ============================================================================
#  Innate OS — robot settings
# ============================================================================
#  Uncomment a line and change its value to override a default.
#  Anything left commented keeps the package default shown.
#  After editing, restart the robot app for changes to take effect.
#
#  Format:   name: value     # [range]  description
# ============================================================================
"""


def _fmt(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def _range_str(e: dict) -> str:
    if "min" in e and "max" in e:
        return f"[{_fmt(e['min'])}, {_fmt(e['max'])}]"
    if e.get("choices"):
        return "{" + ", ".join(_fmt(c) for c in e["choices"]) + "}"
    return ""


def render_template(active: dict = None) -> str:
    """Render the self-documenting friendly overrides file from the registry.

    Every knob is emitted as a commented line showing its default, range, and
    description. Names present in ``active`` (a ``{name: value}`` mapping) are
    emitted uncommented so existing overrides survive a regeneration.
    """
    active = active or {}
    entries = load_registry()
    name_w = max((len(e["name"]) for e in entries), default=0) + 1  # + ':'
    # Cap so a single long string value (a URL/UUID) does not pad every numeric
    # row; longer values simply overflow and push their own comment right.
    val_w = min(max((len(_fmt(e.get("default"))) for e in entries), default=0), 9)

    lines = [_TEMPLATE_HEADER.rstrip("\n")]
    last_group = None
    for e in entries:
        group = e["group"]
        if group != last_group:
            lines.append("")
            lines.append(f"# ── {group} " + "─" * max(1, 58 - len(group)))
            last_group = group

        key = (e["name"] + ":").ljust(name_w)
        rng = _range_str(e)
        comment = f"# {rng + '  ' if rng else ''}{e['desc']}".rstrip()
        if e["name"] in active:
            value = _fmt(active[e["name"]]).ljust(val_w)
            lines.append(f"{key} {value}  {comment}")
        else:
            value = _fmt(e.get("default")).ljust(val_w)
            lines.append(f"# {key} {value}  {comment}")

    return "\n".join(lines) + "\n"
