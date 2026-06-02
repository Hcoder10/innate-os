# CI (`ci/`)

How innate-os is built and tested in CI, and *why* it's wired this way. For the
gate-by-gate breakdown of what runs, see [`../TESTING.md`](../TESTING.md).

## Layout

| File | Role |
|---|---|
| `Dockerfile.test` | The CI image: builds the whole `ros2_ws` (all packages) + bakes in `config/`, `workspace/`, and the test script. |
| `Dockerfile.test.dockerignore` | Build context for the above (allowlist of what the image COPYs). |
| `run_integration_tests.sh` | **Single source of truth for "the tests."** Runs unit (pytest) + integration (`colcon test`) inside the image. |
| `docker-compose.test.yml` | Runs `run_integration_tests.sh` locally / on a self-hosted box. |
| `cloudbuild.integration-test.yaml` | Runs the *same* script on Google Cloud Build. |

The GitHub workflows live in `../.github/workflows/` (`format.yml`, `integration-test.yml`).

## Why tests run on Cloud Build

`integration-test.yml` runs on a GitHub-hosted runner that does **no Docker work**
— it just authenticates to GCP via **Workload Identity Federation** (OIDC, no
service-account keys) and `gcloud builds submit`s the heavy build+test to **Cloud
Build** (`E2_HIGHCPU_8`). This keeps the slow ROS image build off any single
machine while staying on GCP. The build and the run-tests step both use the
documented `gcr.io/cloud-builders/docker` form.

`run_integration_tests.sh` is shared so local and CI run the identical flow —
there is no second definition of "the tests" to drift.

## Why Zenoh shared memory is disabled in CI

This is the non-obvious one. `rmw_zenoh` allocates a **POSIX shared-memory
provider at `rclpy.init`** (~48 MiB/node). Cloud Build's default workers run under
**gVisor, which blocks POSIX shm** — so every ROS node dies at startup with
`Failed to create POSIX SHM provider` (`OS error 12`), *even with a 2 GB
`/dev/shm`*. It is not a `/dev/shm` size problem.

So `run_integration_tests.sh` exports `ZENOH_*_CONFIG_OVERRIDE=...shared_memory/enabled=false`
for the test run. These are **correctness tests** (orchestration logic, not
transport), so loopback is equivalent to SHM. The **robot keeps SHM in production**
— it's enabled by the image entrypoint, which the test script overrides only for
itself.

`--shm-size=2g` in `cloudbuild.integration-test.yaml` and the optional
`GCP_CLOUD_BUILD_WORKER_POOL` (a private, non-gVisor worker pool) are belt-and-
suspenders: they only matter if SHM is ever re-enabled in CI.

## Why the build covers every package

`Dockerfile.test` builds **all** packages (no `--packages-skip`). `maurice_cam`/
`nav`/`control` were once skipped as "Jetson-only", but they compile fine off
Jetson (cam's VPI stereo is `if(VPI_FOUND)`-guarded; nav's `cupy` is runtime-only).
Building everything makes the build the broad safety net for structural refactors.

## Running it locally

```bash
INNATE_TEST_IMAGE=innate-os-test:latest docker compose -f ci/docker-compose.test.yml up \
  --abort-on-container-exit --exit-code-from integration-test
# (build the image first: docker build -f ci/Dockerfile.test -t innate-os-test:latest .)
```
