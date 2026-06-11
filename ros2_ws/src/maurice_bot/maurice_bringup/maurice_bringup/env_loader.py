#!/usr/bin/env python3
"""
Environment loader for Innate-OS.
Loads .env file and provides access to environment variables.
"""

import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

ENV_KEYS_MOVED_TO_OS_CONFIG = {
    "BRAIN_WEBSOCKET_URI",
    "TELEMETRY_URL",
    "CARTESIA_VOICE_ID",
}


def _load_key_value_env(path: Path) -> None:
    if not path.exists():
        return
    try:
        f = open(path)
    except OSError as e:
        # Transient mount errors (e.g. Docker Desktop single-file bind EPERM)
        # would otherwise crash launch-file loading. Treat as "no env file".
        print(f"[env_loader] Could not open {path}: {e}", file=sys.stderr)
        return
    with f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                if key in ENV_KEYS_MOVED_TO_OS_CONFIG:
                    continue
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ[key] = value


def _strip_toml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    chars: list[str] = []

    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\" and (in_single or in_double):
            chars.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            chars.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            chars.append(char)
            continue
        if char == "#" and not in_single and not in_double:
            break
        chars.append(char)

    return "".join(chars).strip()


def _parse_toml_array(value: str) -> list[str]:
    """Parse a single-line inline TOML array of strings, e.g. ``["/a", "/b"]``.

    The hand-rolled fallback parser is line-based, so only single-line arrays are
    supported (which is all the documented ``[paths]`` usage needs). Without this,
    a user writing TOML array syntax on Python 3.10 (no tomllib) would get the raw
    ``["/a", "/b"]`` string and the dirs would silently fail to load.
    """
    items: list[str] = []
    for item in value[1:-1].split(","):
        item = item.strip()
        if not item:
            continue
        if len(item) >= 2 and item[0] == item[-1] and item[0] in {"'", '"'}:
            item = item[1:-1]
        items.append(item)
    return items


def _parse_toml_scalar(raw_value: str):
    value = _strip_toml_comment(raw_value).strip()
    if len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        return _parse_toml_array(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _parse_toml_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        if tomllib is not None:
            with path.open("rb") as f:
                data = tomllib.load(f)
            return data if isinstance(data, dict) else {}
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[env_loader] Could not read {path}: {e}", file=sys.stderr)
        return {}

    data: dict = {}
    current_section: dict = data
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            if not section_name:
                continue
            current_section = data
            for key in section_name.split("."):
                current_section = current_section.setdefault(key, {})
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        current_section[key] = _parse_toml_scalar(raw_value)
    return data


def _join_dirs(value) -> str:
    """Normalize a ``[paths]`` entry to an os.pathsep-joined string of dirs.

    Accepts either an os.pathsep-separated string (the documented form, which
    parses identically with or without ``tomllib``) or a list (when ``tomllib``
    parses a TOML array). Blank entries are dropped.
    """
    if isinstance(value, str):
        parts = value.split(os.pathsep)
    elif isinstance(value, (list, tuple)):
        parts = [str(p) for p in value]
    else:
        return ""
    return os.pathsep.join(p.strip() for p in parts if p.strip())


def _warn_missing_dirs(joined: str, label: str) -> None:
    """Warn about configured dirs that don't exist, so a typo in os.toml isn't silent."""
    for part in joined.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        expanded = os.path.expandvars(os.path.expanduser(part))
        if not os.path.isdir(expanded):
            print(f"[env_loader] Configured {label} directory does not exist: {expanded}", file=sys.stderr)


def _load_os_config(path: Path) -> None:
    if not path.exists():
        return

    data = _parse_toml_file(path)

    brain = data.get("brain", {}) if isinstance(data, dict) else {}
    telemetry = data.get("telemetry", {}) if isinstance(data, dict) else {}
    voice = data.get("voice", {}) if isinstance(data, dict) else {}
    paths = data.get("paths", {}) if isinstance(data, dict) else {}

    websocket_uri = brain.get("websocket_uri") if isinstance(brain, dict) else None
    telemetry_url = telemetry.get("url") if isinstance(telemetry, dict) else None
    cartesia_voice_id = voice.get("cartesia_voice_id") if isinstance(voice, dict) else None
    agent_dirs = paths.get("agent_dirs") if isinstance(paths, dict) else None
    skill_dirs = paths.get("skill_dirs") if isinstance(paths, dict) else None

    if isinstance(websocket_uri, str) and websocket_uri.strip():
        os.environ.setdefault("BRAIN_WEBSOCKET_URI", websocket_uri.strip())
    if isinstance(telemetry_url, str) and telemetry_url.strip():
        os.environ.setdefault("TELEMETRY_URL", telemetry_url.strip())
    if isinstance(cartesia_voice_id, str) and cartesia_voice_id.strip():
        os.environ.setdefault("CARTESIA_VOICE_ID", cartesia_voice_id.strip())
    if joined_agent_dirs := _join_dirs(agent_dirs):
        os.environ.setdefault("INNATE_EXTRA_AGENT_DIRS", joined_agent_dirs)
        _warn_missing_dirs(joined_agent_dirs, "agent")
    if joined_skill_dirs := _join_dirs(skill_dirs):
        os.environ.setdefault("INNATE_EXTRA_SKILL_DIRS", joined_skill_dirs)
        _warn_missing_dirs(joined_skill_dirs, "skill")


def load_env_file(env_path: Path | None = None) -> None:
    """
    Load environment variables from .env file.

    Args:
        env_path: Optional path to .env file. If not provided, uses INNATE_OS_ROOT
                  or defaults to ~/innate-os/.env
    """
    if env_path is None:
        innate_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
        env_path = Path(innate_root) / ".env"

    innate_root = env_path.parent
    _load_os_config(innate_root / "config" / "os.toml")
    _load_key_value_env(env_path)


def get_env(key: str, default: str = "") -> str:
    """
    Get environment variable, loading .env if not already loaded.

    Args:
        key: Environment variable name
        default: Default value if not found

    Returns:
        Environment variable value or default
    """
    return os.environ.get(key, default)


# Load env file on module import
if __name__ == "__main__":
    load_env_file()
