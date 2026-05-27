# Summary: `refactor-sim` cleanup

The branch's goal was to declutter the repo root and remove dead weight, while folding the simulator into the monorepo behind a single `./innate` entry point. Every change traces back to one of three principles: **one obvious home for each kind of file**, **delete what isn't pulling its weight**, **secrets and overrides are user-owned, not repo-owned**.

## Root cleanup — fewer top-level entries

| Before (root) | After |
|---|---|
| `Dockerfile`, `Dockerfile.prod`, `Dockerfile.test` | `sim/Dockerfile` (only one that's actually built locally); `ci/Dockerfile.test` |
| `Makefile` | deleted — every target was either dead or shadowed by `./innate` / `docker compose` |
| `docker-compose.prod.yml` | deleted — prod is built and deployed via Cloud Build, not compose |
| `docker-compose.dev.yml` | `sim/docker-compose.dev.yml` (it's a sim concern, not a root concern) |
| `set_build_mode.sh` | deleted — mode is now resolved at launch via `print_runtime_env.py` |
| `.flake8` | deleted — `pyproject.toml` owns Python style; keeping a flake8 file alongside it just creates drift |
| `os_config.yaml` | deleted — single-key file (`minimum_app_version`) that nothing was reading |
| `maps/.gitignore` | deleted — duplicated patterns already covered by the root `.gitignore` |

## `config/` — one home for all system-level non-code

Previously scattered as `dds/`, `udev/`, `systemd/`, `sounds/`, `zshrcs/` at the repo root. Merged under `config/`:

- `config/dds/` — Zenoh + DDS setup
- `config/udev/` — `99-rplidar.rules`
- `config/systemd/` — all five `.service` units
- `config/sounds/` — startup/shutdown chimes
- `config/zsh/` — robot `.zshrc`
- `config/os.toml.template` — **new**: non-secret runtime overrides (brain URI, telemetry URL, voice ID). User copies to `config/os.toml` (git-ignored) to override built-in defaults.

Result: the root is no longer a soup of single-purpose directories, and there's now a clear distinction between *system config the OS ships* (`config/`) and *user overrides* (`config/os.toml`, `.env`).

## `.env` is now secrets only

`.env.template` was trimmed to the keys that are actually secret (`INNATE_SERVICE_KEY`, optional `CARTESIA_VOICE_ID`). Everything that was just a default URL moved to `config/os.toml.template`. This kills the most common foot-gun: people editing `.env` to change a hostname and then committing secrets by accident.

## `workspace/` — user-extensible code in one place

> **Naming TBD:** Currently called `workspace/` — the name is **still up for discussion**. Earlier iterations used `extensions/`. The directory's purpose is clear; what we call it isn't locked in. Open to alternatives (`user/`, `plugins/`, `apps/`, keep `workspace/`, revert to `extensions/`).

Three sibling top-level directories (`agents/`, `inputs/`, `skills/`) became `workspace/`. They share the same contract (pure-Python, auto-discovered, no ROS imports), so they belong together. This also surfaces the fact that `inputs/` is genuinely sparse — easier to see and fix once it's next to its siblings. Docs in `docs/INPUT_DEVICES.md` updated to match the new paths.

## `sim/` — simulator absorbed into the monorepo

> **Note:** The `sim/` import itself is **Axel's work, not part of this PR's review scope** — it landed earlier on the branch and is being carried along. The changes called out here are just the *integration points* this cleanup touched (the `./innate` shim, the docker context move, the launch-script wiring). Review of `sim/` internals belongs to Axel's PR; nothing in this cleanup modifies sim semantics.

`sim/` now contains the full simulator stack (FastAPI backend, React frontend, Genesis bindings, Dockerfile, requirements, asset lock). The new top-level `innate` script is a 5-line shim that delegates to `sim/launcher/main.py`, giving a single command surface: `./innate setup`, `./innate sim`, etc.

This is also why `docker-compose.dev.yml` and the local-build `Dockerfile` moved under `sim/` — they're the simulator's dev environment, not the OS's.

## CI / Docker context

- `ci/` collects test-only Docker assets (`ci/Dockerfile.test`, `ci/docker-compose.test.yml`, `ci/cloudbuild.sim-images.yaml`).
- New `.dockerignore` and `.gcloudignore` keep build contexts tiny — only dependency manifests get sent to the daemon; source is bind-mounted at runtime.
- New workflows (`publish-sim-images.yml`, `cleanup-sim-images.yml`, `sim-assets.yml`) wire the sim image lifecycle into CI.

## Docs pruned

Removed `docs/DEPENDENCIES.md` (409 lines), `docs/GIT_RECOVERY.md`, `docs/SIMULATION_MODE.md`, `docs/SYSTEM_SETUP.md` — all either out of date or duplicating what's now in the launcher / CLAUDE.md / sim README. `docs/SYSTEM_OVERVIEW.md` and `docs/INPUT_DEVICES.md` updated for the new paths. Root `CLAUDE.md` added to give agents the same behavioral guardrails the human readers expect.

## Open follow-ups (not yet done on this branch)

- **`ros2_ws/src` hoisted up**: not addressed — `src/` still lives inside `ros2_ws/` alongside `build/`/`install/`. Worth a separate PR if you want to pursue it (it touches every colcon path).
- **`dev/launcher`**: still present, not yet evaluated.
- **`workspace/inputs/` still sparse**: only `arm_vitals_input.py` and `micro_input.py`. The rename made the under-population visible but didn't decide between "fill it" and "fold it in".
- **`config/zsh/`**: kept under `config/` for now since systemd units reference it; if you want it gone from the repo entirely it should move to a provisioning role.

---

**Net effect:** 108 files changed outside `sim/` (+1871 / −1635), and the root directory listing dropped from ~25 top-level entries to ~15.
