#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys

if sys.version_info < (3, 10):  # noqa: UP036
    print("Error: the Innate launcher requires Python 3.10 or newer.", file=sys.stderr)
    raise SystemExit(1)

from assets import pack_assets, publish_assets, validate_assets
from config import (
    CLI_ROOT,
    CLI_SIM,
    ENV_PATH,
    HOSTED_MODE,
    LOCAL_MODES,
    LOG_TARGETS,
    OS_CONFIG_PATH,
    OS_SESSION_LOG_PATH,
    SHOW_LIVE_DASHBOARD_DEFAULT,
    SIM_CONFIG_PATH,
    STATE_DIR,
    StackError,
    build_cloud_env,
    build_os_env,
    get_config,
    log,
    success,
    warn,
)
from dashboard import (
    BOLD,
    NC,
    DashboardCallbacks,
    DashboardOptions,
    print_banner,
    print_status,
    watch_dashboard,
)
from runtime import (
    available_agent_count,
    capture_agent_logs,
    capture_os_brain_logs,
    capture_simulator_logs,
    clean_runtime,
    collect_status_snapshot,
    config_frontend_port,
    config_simulator_port,
    down_cloud_agent,
    down_os,
    ensure_docker_available,
    ensure_frontend_container,
    ensure_os_container,
    ensure_sim_data,
    ensure_sim_setup,
    ensure_skill_assets,
    open_os_container_shell,
    prebuild_frontend_image,
    print_startup_checks,
    runtime_already_running,
    set_simulator_log_mode,
    start_cloud_agent,
    start_simulator,
    stop_simulator,
    tail_file,
    wait_for_brain_directives,
    wait_for_frontend_ready,
    wait_for_os_runtime_ready,
    wait_for_simulator_http,
)
from setup_wizard import _prompt_yes_no, configure_hosted_service_key, is_interactive_terminal

DASHBOARD_OPTIONS = DashboardOptions(
    hosted_mode=HOSTED_MODE,
    local_modes=LOCAL_MODES,
    cli_sim=CLI_SIM,
    state_dir=STATE_DIR,
)


def dashboard_callbacks() -> DashboardCallbacks:
    return DashboardCallbacks(
        collect_status_snapshot=collect_status_snapshot,
        capture_simulator_logs=capture_simulator_logs,
        capture_os_brain_logs=capture_os_brain_logs,
        capture_agent_logs=capture_agent_logs,
        set_simulator_log_mode=set_simulator_log_mode,
        success=success,
    )


def show_runtime_dashboard(config: dict[str, object], *, watch: bool) -> None:
    if watch and sys.stdout.isatty():
        dashboard_result = watch_dashboard(config, dashboard_callbacks(), DASHBOARD_OPTIONS)
        if dashboard_result == "shutdown":
            print()
            log("Ctrl+C received. Stopping the Innate runtime...")
            cmd_down(config)
    else:
        print_status(config, dashboard_callbacks(), DASHBOARD_OPTIONS)


def cmd_up(
    config: dict[str, object],
    *,
    watch: bool = SHOW_LIVE_DASHBOARD_DEFAULT,
    sim_visualization_override: bool | None = None,
) -> None:
    started = False
    try:
        if sim_visualization_override is not None:
            config = {**config, "sim_visualization": sim_visualization_override}
        ensure_docker_available(command_hint=f"{CLI_SIM} up")
        print_banner()
        if runtime_already_running(config):
            log("Innate sim runtime is already running. Opening dashboard...")
            show_runtime_dashboard(config, watch=watch)
            return

        os_env_file = build_os_env(config)
        cloud_env_file = build_cloud_env(config)
        sim_python = ensure_sim_setup(config)
        ensure_sim_data(config, allow_fetch=False)
        ensure_skill_assets(config)

        started = True
        start_cloud_agent(config, cloud_env_file)
        ensure_os_container(config, os_env_file)
        # Start the web frontend container; its build runs in the background while the
        # simulator loads (we wait on it below).
        ensure_frontend_container(config)
        start_simulator(config, sim_python)

        simulator_port = config_simulator_port(config)
        log("Waiting for the simulator HTTP endpoint...")
        wait_for_simulator_http(
            simulator_port,
            timeout_seconds=float(config["sim_startup_timeout_seconds"]),
        )
        log("Waiting for ROS bridge and brain client...")
        if not wait_for_os_runtime_ready(config):
            print_startup_checks(
                config,
                simulator_http_ready=True,
                brain_directive_count=available_agent_count(simulator_port),
            )
            raise StackError(
                "Simulator backend is up, but the OS ROS bridge/brain client did not become ready.\n"
                f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
            )
        log("Waiting for brain directives...")
        brain_directive_count = wait_for_brain_directives(simulator_port)
        log("Waiting for the web frontend to build...")
        wait_for_frontend_ready(config_frontend_port(config))
        print_startup_checks(
            config,
            simulator_http_ready=True,
            brain_directive_count=brain_directive_count,
        )
        if brain_directive_count <= 0:
            raise StackError(
                "Simulator backend is up, but brain directives never became available.\n"
                f"Recent brain log output:\n{os.linesep.join(capture_os_brain_logs(config, lines=40))}"
            )
        success("Innate sim runtime is up.")
        show_runtime_dashboard(config, watch=watch)
    except KeyboardInterrupt:
        print()
        if started:
            warn("Interrupted. Stopping the Innate runtime...")
            cmd_down(config)
        else:
            warn("Interrupted before the Innate runtime finished starting.")
    except StackError:
        if started:
            warn("Startup failed. Stopping the partially-started Innate runtime...")
            cmd_down(config)
        raise


def cmd_down(config: dict[str, object]) -> None:
    stop_simulator()
    down_cloud_agent()
    down_os(config)
    log("Innate sim runtime is down.")


def _confirm_clean(config: dict[str, object], *, delete_data: bool) -> bool:
    sim_repo = config["sim_repo"]
    print(f"{BOLD}This will permanently delete:{NC}")
    print("  - Docker containers and volumes for the sim runtime")
    print(f"  - local sim venv ({CLI_SIM} setup will rebuild it)")
    if delete_data:
        print(f"  - sim data: {sim_repo / 'data'} (multi-GB ReplicaCAD + asset pack; re-downloadable)")

    if not is_interactive_terminal():
        warn("Refusing to clean without confirmation. Re-run with --yes to proceed non-interactively.")
        return False

    return _prompt_yes_no("Continue?", default=False)


def cmd_clean(config: dict[str, object], *, delete_data: bool = False, assume_yes: bool = False) -> None:
    if not assume_yes and not _confirm_clean(config, delete_data=delete_data):
        warn("Aborted. Nothing was deleted.")
        return

    clean_runtime(config, delete_data=delete_data)
    success("Innate sim runtime cleaned (containers, volumes, and local venv removed).")

    sim_repo = config["sim_repo"]
    print("Preserved (never deleted by clean):")
    print(f"  - secrets:      {ENV_PATH}")
    print(f"  - OS config:    {OS_CONFIG_PATH}")
    print(f"  - sim config:   {SIM_CONFIG_PATH}")
    print(f"  - env presets:  {sim_repo / 'data' / 'environments'}")
    if not delete_data:
        print(f"  - sim data:     {sim_repo / 'data'} (ReplicaCAD + asset pack; use --data to remove)")

    log(f"Run `{CLI_SIM} setup` to rebuild the simulator environment.")


def cmd_logs(target: str) -> None:
    if target == "startup":
        found_logs = False
        for name in ("bootstrap", "frontend", "compose", "cloud-agent", "os-build", "os-session", "simulator"):
            path = LOG_TARGETS[name]
            if path.exists():
                found_logs = True
                print(f"{BOLD}{path}{NC}")
                print(tail_file(path, limit=80))
                print()
        if not found_logs:
            warn("No startup logs have been written yet.")
        return

    if target == "brain":
        config = get_config()
        print("\n".join(capture_os_brain_logs(config, lines=60)))
        return

    path = LOG_TARGETS[target]
    print(tail_file(path, limit=120))


def cmd_setup(config: dict[str, object]) -> None:
    ensure_docker_available(command_hint=f"{CLI_SIM} setup")
    print_banner()
    configure_hosted_service_key(config)
    sim_python = ensure_sim_setup(config)
    ensure_sim_data(config, allow_fetch=True)
    prebuild_frontend_image(config)
    success("Simulator setup is ready.")
    print(f"OS secrets: {ENV_PATH}")
    print(f"OS config: {OS_CONFIG_PATH}")
    print(f"Sim config: {SIM_CONFIG_PATH}")
    print(f"Simulator Python: {sim_python}")


def cmd_assets(args: argparse.Namespace, config: dict[str, object]) -> None:
    sim_repo = config["sim_repo"]  # type: ignore[assignment]
    if args.assets_command == "pack":
        pack_assets(sim_repo, image=args.image, write_lock=args.write_lock)
        return
    if args.assets_command == "publish":
        ensure_docker_available(command_hint=f"{CLI_SIM} assets publish")
        publish_assets(sim_repo, image=args.image)
        return
    if args.assets_command == "validate":
        if args.ci:
            ensure_docker_available(command_hint=f"{CLI_SIM} assets validate --ci")
        validate_assets(
            sim_repo,
            mode="ci" if args.ci else "local",
            staged_only=args.staged_only,
        )
        return
    raise StackError(f"Unknown assets command: {args.assets_command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="innate", description="Innate local development CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sim_parser = subparsers.add_parser(
        "sim",
        prog=f"{CLI_ROOT} sim",
        help="Set up and run the local simulator-backed runtime",
    )
    sim_subparsers = sim_parser.add_subparsers(dest="sim_command", required=True)
    sim_subparsers.add_parser(
        "setup",
        prog=f"{CLI_SIM} setup",
        help="Prepare the simulator environment, frontend build, scene data, and credentials",
    )
    up_parser = sim_subparsers.add_parser(
        "up",
        prog=f"{CLI_SIM} up",
        help="Start the local simulator-backed runtime",
    )
    up_parser.add_argument(
        "--once",
        action="store_true",
        help="Start the runtime and print a single status snapshot instead of the live dashboard",
    )
    up_parser.add_argument(
        "--vis",
        action="store_true",
        help="Start the simulator with the native visualization window enabled for this run",
    )
    sim_subparsers.add_parser(
        "down",
        prog=f"{CLI_SIM} down",
        help="Stop the local simulator-backed runtime",
    )
    clean_parser = sim_subparsers.add_parser(
        "clean",
        prog=f"{CLI_SIM} clean",
        help="Stop the runtime and delete related Docker containers/volumes and the local sim venv",
    )
    clean_parser.add_argument(
        "--data",
        action="store_true",
        help="Also delete downloaded ReplicaCAD datasets and the simulator asset pack",
    )
    clean_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for non-interactive/scripted use)",
    )
    sim_subparsers.add_parser(
        "sh",
        prog=f"{CLI_SIM} sh",
        help="Open an interactive shell inside the running ROS container",
    )
    status_parser = sim_subparsers.add_parser(
        "status",
        prog=f"{CLI_SIM} status",
        help="Show current runtime status",
    )
    status_parser.add_argument(
        "mode",
        nargs="?",
        default="panel",
        choices=["panel", "verbose"],
        help="Show the default panel or include extra repo/runtime details",
    )
    logs_parser = sim_subparsers.add_parser(
        "logs",
        prog=f"{CLI_SIM} logs",
        help="Show recent logs",
    )
    logs_parser.add_argument(
        "target",
        nargs="?",
        default="simulator",
        choices=[
            "startup",
            "bootstrap",
            "frontend",
            "compose",
            "cloud-agent",
            "os-build",
            "os-session",
            "simulator",
            "brain",
            "down",
        ],
        help="Which log stream to show",
    )
    assets_parser = sim_subparsers.add_parser(
        "assets",
        prog=f"{CLI_SIM} assets",
        help="Manage simulator asset packs",
    )
    assets_subparsers = assets_parser.add_subparsers(dest="assets_command", required=True)
    pack_parser = assets_subparsers.add_parser(
        "pack",
        prog=f"{CLI_SIM} assets pack",
        help="Compute the simulator asset pack lock from local asset files",
    )
    pack_parser.add_argument("--image", default="ghcr.io/innate-inc/innate-os-sim-assets")
    pack_parser.add_argument(
        "--write-lock",
        action="store_true",
        help="Write sim/assets.lock.json after computing the asset hash",
    )
    publish_parser = assets_subparsers.add_parser(
        "publish",
        prog=f"{CLI_SIM} assets publish",
        help="Build and push the simulator asset image to GHCR",
    )
    publish_parser.add_argument("--image", default="ghcr.io/innate-inc/innate-os-sim-assets")
    validate_parser = assets_subparsers.add_parser(
        "validate",
        prog=f"{CLI_SIM} assets validate",
        help="Validate simulator asset references and locked content",
    )
    validate_parser.add_argument(
        "--ci",
        action="store_true",
        help="Pull the locked asset image and verify it in an isolated directory",
    )
    validate_parser.add_argument(
        "--local",
        action="store_true",
        help="Validate local files only (default)",
    )
    validate_parser.add_argument(
        "--staged-only",
        action="store_true",
        help="Skip validation unless staged files touch simulator asset references",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:])

    try:
        config = get_config()

        if args.command != "sim":
            parser.error(f"Unknown command group: {args.command}")

        if args.sim_command == "setup":
            cmd_setup(config)
        elif args.sim_command == "up":
            cmd_up(
                config,
                watch=not args.once,
                sim_visualization_override=True if args.vis else None,
            )
        elif args.sim_command == "down":
            ensure_docker_available(command_hint=f"{CLI_SIM} down")
            cmd_down(config)
        elif args.sim_command == "clean":
            ensure_docker_available(command_hint=f"{CLI_SIM} clean")
            cmd_clean(config, delete_data=args.data, assume_yes=args.yes)
        elif args.sim_command == "sh":
            ensure_docker_available(command_hint=f"{CLI_SIM} sh")
            return open_os_container_shell()
        elif args.sim_command == "status":
            ensure_docker_available(command_hint=f"{CLI_SIM} status")
            print_status(
                config,
                dashboard_callbacks(),
                DASHBOARD_OPTIONS,
                verbose=args.mode == "verbose",
            )
        elif args.sim_command == "logs":
            cmd_logs(args.target)
        elif args.sim_command == "assets":
            cmd_assets(args, config)
        else:
            parser.error(f"Unknown sim command: {args.sim_command}")
    except StackError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return exc.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
