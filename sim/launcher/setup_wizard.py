from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from config import (
    CLOUD_AGENT_DIR_NAME,
    CLOUD_AGENT_GIT_URL,
    ENV_PATH,
    GEMINI_API_KEY,
    WORKSPACE_ROOT,
    is_configured_secret_value,
    log,
    success,
    warn,
)
from dashboard import BOLD, CYAN, DIM, GREEN, NC, YELLOW

INNATE_SERVICE_KEY = "INNATE_SERVICE_KEY"


def is_interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def is_configured_secret(value: str | None) -> bool:
    return is_configured_secret_value(INNATE_SERVICE_KEY, value)


def _is_active_env_assignment(line: str, key: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    assignment_key, _ = stripped.split("=", 1)
    return assignment_key.strip() == key


def _prompt_yes_no(question: str, *, default: bool = False) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{YELLOW}{question} [{default_label}]: {NC}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)  # noqa: B904
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print(f"{YELLOW}Please enter y or n.{NC}")


def _prompt_secret(question: str) -> str:
    try:
        return getpass.getpass(f"{YELLOW}{question}: {NC}", stream=sys.stdout).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        raise SystemExit(1)  # noqa: B904


def _quote_env_value(value: str) -> str:
    if "'" in value:
        raise ValueError("secret values saved to .env cannot contain single quotes")
    return f"'{value}'"


def write_env_value(path: Path, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} cannot contain newlines")

    replacement = f"{key}={_quote_env_value(value)}"
    lines = path.read_text().splitlines() if path.exists() else []
    updated = False
    output: list[str] = []

    for line in lines:
        if _is_active_env_assignment(line, key):
            if not updated:
                output.append(replacement)
                updated = True
        else:
            output.append(line)

    if not updated:
        if output and output[-1].strip():
            output.append("")
        output.append(replacement)

    path.write_text("\n".join(output) + "\n")


def comment_out_env_key(path: Path, key: str) -> bool:
    """Comment out an active ``KEY=...`` assignment in the env file, if present.

    Returns True when a line was actually commented out. Leaves unset keys and
    already-commented lines untouched.
    """
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    changed = False
    output: list[str] = []
    for line in lines:
        if _is_active_env_assignment(line, key):
            output.append(f"# {line}")
            changed = True
        else:
            output.append(line)
    if changed:
        path.write_text("\n".join(output) + "\n")
    return changed


def _save_service_key(config: dict[str, object], service_key: str) -> None:
    write_env_value(ENV_PATH, INNATE_SERVICE_KEY, service_key)
    _use_service_key_for_run(config, service_key)
    success(f"Saved {INNATE_SERVICE_KEY} to {ENV_PATH}.")


def _use_service_key_for_run(config: dict[str, object], service_key: str) -> None:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[INNATE_SERVICE_KEY] = service_key
    user_env[INNATE_SERVICE_KEY] = service_key


def _prompt_choice(question: str, options: dict[str, str], *, default: str) -> str:
    print(f"{YELLOW}{question}{NC}")
    for key, label in options.items():
        marker = "  (default)" if key == default else ""
        print(f"  {BOLD}{key}{NC}) {label}{DIM}{marker}{NC}")
    while True:
        try:
            value = input(f"{YELLOW}Choose [{default}]: {NC}").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)  # noqa: B904
        if not value:
            return default
        if value in options:
            return value
        print(f"{YELLOW}Please choose one of: {', '.join(options)}.{NC}")


def _save_gemini_key(config: dict[str, object], gemini_key: str) -> None:
    write_env_value(ENV_PATH, GEMINI_API_KEY, gemini_key)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[GEMINI_API_KEY] = gemini_key
    user_env[GEMINI_API_KEY] = gemini_key
    success(f"Saved {GEMINI_API_KEY} to {ENV_PATH}.")


def _configure_gemini_key(config: dict[str, object]) -> None:
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY)):
        success(f"{GEMINI_API_KEY} already configured.")
        return

    shell_value = os.environ.get(GEMINI_API_KEY, "").strip()
    if is_configured_secret_value(GEMINI_API_KEY, shell_value) and _prompt_yes_no(
        f"Found {GEMINI_API_KEY} in your shell. Save it to {ENV_PATH.name}?", default=True
    ):
        _save_gemini_key(config, shell_value)
        return

    while True:
        gemini_key = _prompt_secret(f"Paste {GEMINI_API_KEY}")
        if is_configured_secret_value(GEMINI_API_KEY, gemini_key):
            _save_gemini_key(config, gemini_key)
            return
        warn("Gemini key cannot be empty. Press Ctrl+C to cancel.")


def _configure_service_key(config: dict[str, object]) -> None:
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret(user_env.get(INNATE_SERVICE_KEY)):
        success(f"{INNATE_SERVICE_KEY} already configured.")
        return

    shell_value = os.environ.get(INNATE_SERVICE_KEY, "").strip()
    if is_configured_secret(shell_value) and _prompt_yes_no(
        f"Found {INNATE_SERVICE_KEY} in your shell. Save it to {ENV_PATH.name}?", default=True
    ):
        _save_service_key(config, shell_value)
        return

    while True:
        service_key = _prompt_secret(f"Paste {INNATE_SERVICE_KEY}")
        if is_configured_secret(service_key):
            _save_service_key(config, service_key)
            print(f"{GREEN}Hosted brain credentials are ready.{NC}")
            return
        warn("Service key cannot be empty. Press Ctrl+C to cancel.")


def ensure_cloud_agent_repo(config: dict[str, object]) -> None:
    """Clone the cloud-agent source next to the repo if it isn't there yet."""
    existing = config.get("cloud_repo")
    if existing and Path(str(existing)).exists():
        success(f"Cloud-agent source present at {existing}.")
        return
    target = WORKSPACE_ROOT / CLOUD_AGENT_DIR_NAME
    if target.exists():
        success(f"Cloud-agent source present at {target}.")
        return
    log(f"Cloning innate-cloud-agent into {target}...")
    result = subprocess.run(
        ["git", "clone", CLOUD_AGENT_GIT_URL, str(target)],
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        success(f"Cloned innate-cloud-agent to {target}.")
    else:
        warn(
            f"Could not clone {CLOUD_AGENT_GIT_URL}. Clone it manually to {target} "
            "(needs GitHub SSH access to innate-inc)."
        )


def _disable_keys(config: dict[str, object], keys: list[str]) -> None:
    """Comment out the given keys in .env (only if currently configured) so the
    selected backend isn't overridden by a leftover key, and forget them for this
    run."""
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    for key in keys:
        if comment_out_env_key(ENV_PATH, key):
            success(f"Commented out {key} in {ENV_PATH.name}.")
        raw_env.pop(key, None)
        user_env.pop(key, None)


def configure_brain_backend(config: dict[str, object]) -> None:
    """Pick the brain backend and collect the matching key.

    Local needs a Gemini key (and the cloud-agent source); hosted needs an Innate
    service key; none runs the sim without an agent. Non-interactively, just
    report what auto-detection will pick.
    """
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    has_gemini = is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY))
    has_service_key = is_configured_secret(user_env.get(INNATE_SERVICE_KEY))

    if not is_interactive_terminal():
        if has_gemini:
            ensure_cloud_agent_repo(config)
            success("Local brain selected (GEMINI_API_KEY detected).")
        elif has_service_key:
            success("Hosted brain selected (INNATE_SERVICE_KEY detected).")
        else:
            warn(
                "No brain backend configured. Add GEMINI_API_KEY (local brain) or "
                f"INNATE_SERVICE_KEY (hosted) to {ENV_PATH}."
            )
        return

    print()
    print(f"{CYAN}{BOLD}Brain Backend{NC}")
    print(
        f"{DIM}The brain is the robot's agent. Run it locally with a Gemini key, "
        f"use Innate's hosted brain with a service key, or run the sim with no agent.{NC}"
    )
    print()
    default_choice = "1" if has_gemini else ("2" if has_service_key else "3")
    choice = _prompt_choice(
        "Which brain backend?",
        {
            "1": "Local cloud-agent (Gemini key)",
            "2": "Hosted Innate brain (service key)",
            "3": "None (run the sim without an agent)",
        },
        default=default_choice,
    )
    if choice == "1":
        _configure_gemini_key(config)
        ensure_cloud_agent_repo(config)
        _disable_keys(config, [INNATE_SERVICE_KEY])
    elif choice == "2":
        _configure_service_key(config)
        _disable_keys(config, [GEMINI_API_KEY])
    else:
        _disable_keys(config, [GEMINI_API_KEY, INNATE_SERVICE_KEY])
        warn("No brain backend selected. The sim will run without an agent.")
