# Simulator image build: runner cost analysis

**Date:** 2026-07-13
**Scope:** Cost/architecture of `publish-sim-images.yml` (multi-arch sim images).
**Question:** Can we drop the split Cloud Build (amd64) + native GH (arm64) setup
for something less ugly, and what does each option cost?

> All per-minute rates below are from research as of 2026-07-13 and **will drift** —
> re-check the Cloud Build and GitHub larger-runner pricing pages before acting on
> the dollar figures. The *relative* conclusions (Cloud Build ~2× cheaper per
> core-min; QEMU multiplies cost) are stable; the absolute numbers are not.

## TL;DR

- Current split isn't a cost problem — it's a **maintenance/aesthetic** problem: two
  build paths (Cloud Build plumbing for amd64, native GH for arm64) around one
  already-shared script, `ci/build_sim_images.sh`.
- **Recommended fix:** collapse both arches into a single GH Actions **matrix** over
  the same script. One code path, symmetric, no GCP dependency. Keep the `manifest`
  job (it's inherent to multi-arch, not part of the "two paths" problem).
- **Cost of that fix:** the amd64 leg moves off Cloud Build (~2× cheaper) onto a GH
  larger runner, costing roughly **+$45/month at ~300 builds/month** — and rising as
  commit volume grows.
- **Do NOT** unify via QEMU on one big runner: emulation multiplies the arm64 build
  time (~3–8×), making it the most expensive *and* slowest option.
- Unifying the other direction (both on Cloud Build) isn't possible — Cloud Build's
  default pool has no native arm64, so it would fall back to QEMU too.

## Empirical build times

From `publish-sim-images.yml` run history (GitHub Actions API, "Submit Cloud Build"
step duration), amd64 on Cloud Build `E2_HIGHCPU_8` (8 vCPU):

| build type              | time        |
| ----------------------- | ----------- |
| warm (cache hit)        | ~5 min (4.4–5.8) |
| cold / full             | ~11–14 min  |
| incremental (cpp change → sim-ros layer rebuilds, deps cached) | ~7–11 min |

Assumption (per project owner): **native arm64 build time ≈ amd64** on comparable
cores. arm64 was never measured because the multi-arch workflow hadn't run on a
triggering branch yet at time of writing.

## Pricing logic

The whole comparison reduces to **$ per vCPU-minute × minutes**, where minutes scale
roughly inversely with core count for the parallel colcon build (with diminishing
returns past ~16 cores).

### Cloud Build (amd64 today)

- Machine type `E2_HIGHCPU_8` bills at a flat **~$0.016 / build-minute** for the whole
  8-vCPU machine → **~$0.002 / vCPU-min**.
- No free tier applies here: the free 120 build-min/day only covers the default
  `e2-medium` machine, not `E2_HIGHCPU_8`.
- Disk (`diskSizeGb: 200`) is **not** billed separately — the per-minute rate is flat
  per machine type.
- GCS source staging + egress to ghcr are negligible (fractions of a cent).

### GitHub-hosted larger runners

- Linux **x64** larger runner = **$0.004 / vCPU-min** (i.e. 8-core = $0.032/min,
  16-core = $0.064/min, 32-core = $0.128/min). This is **~2× the Cloud Build
  per-vCPU-min rate.**
- Linux **arm64** larger runners are ~30–40% cheaper than the x64 equivalent (verify).
- **Standard** 2-/4-core runners are **free on public repos** (`innate-inc/innate-os`
  is public) — but the sim build needs more cores/disk than standard runners give, so
  standard/free runners don't apply to the heavy build legs. They *do* make the tiny
  `manifest` job free.

### QEMU penalty

Building arm64 on an x64 runner via QEMU user-mode emulation runs a heavy C++/colcon
build **~3–8× slower per instruction**. More cores don't buy this back — the
bottleneck is emulation, not parallelism. So a native ~5 min build at 32 cores becomes
~15–40 min emulated, at the big-runner rate. You pay the premium machine rate **and**
the time multiplier.

## Cost per run

### amd64 leg only (the part that could move GCP → GH)

| build     | Cloud Build `E2_HIGHCPU_8` | GH 8-core x64 |
| --------- | -------------------------- | ------------- |
| warm      | ~$0.08                     | ~$0.16        |
| cold      | ~$0.18–0.22                | ~$0.36–0.42   |
| incremental | ~$0.15                   | ~$0.30        |

### Whole multi-arch run

Two heavy legs (amd64 + arm64) run in parallel, plus a near-free `manifest` stitch.
Total ≈ ~2× the single-leg figure. The arm64 leg is paid on GitHub regardless of what
amd64 runs on.

### Unification options compared (cold build, heavy runners)

| option                          | arm64 leg      | wall-clock | $/run |
| ------------------------------- | -------------- | ---------- | ----- |
| Native GH matrix, 16-core each  | native, ~7 min | ~7 min     | ~$0.90 |
| Native GH matrix, 32-core each  | native, ~5 min | ~5 min     | ~$1.30 |
| QEMU on one 32-core runner      | emulated, ~15–40 min | ~15–40 min | ~$3–8 |

QEMU is ~5–8× the cost and several times slower. Its *only* benefit is YAML simplicity
(one job, no matrix, no manifest) — an expensive way to buy tidiness.

## Volume projection

`git log` on `main`: **~360 commits/month**, most touching C++. With `cancel-in-progress`
concurrency, not all complete → estimate **~250–300 builds/month**, and **growing** as
commit cadence increases.

Monthly, amd64 leg only:

| builds/mo | Cloud Build (~$0.15/run) | GH 8-core (~$0.30/run) | delta      |
| --------- | ------------------------ | ---------------------- | ---------- |
| 150       | ~$22                     | ~$45                   | ~$23/mo    |
| 300       | ~$45                     | ~$90                   | ~$45/mo    |

The 2× per-core-min gap compounds with volume: as commits grow, keeping amd64 on
Cloud Build saves more each month. At 300 builds/mo the split is worth ~$45/mo
(~$540/yr) — the *price* of keeping the ugly-but-cheap path.

## Bigger levers than the runner choice

At every-cpp-commit cadence, two changes save more than any runner swap:

1. **Don't build arm64 on every commit.** If arm64 ships only on `main` merges /
   releases while amd64 covers per-commit CI, the whole bill roughly halves. Biggest
   single win.
2. **Self-hosted runners.** At 250–300+ builds/month a fixed box (one amd64 + one
   arm64) amortizes to near-zero marginal cost per build and beats both Cloud Build and
   GH larger runners. Trades cloud spend for hardware + ops.

## Recommendation

Collapse the two heavy legs into one matrix over the existing `ci/build_sim_images.sh`
(already arch-agnostic via `ARCH`). This deletes the entire Cloud Build path —
`ci/cloudbuild.sim-images.yaml`, GCP auth steps, Secret Manager secrets, GCP repo vars
— and leaves one symmetric build path plus the unchanged `manifest` stitch:

```yaml
build:
  needs: tags
  strategy:
    matrix:
      include:
        - { arch: amd64, runner: <8- or 16-core x64 larger runner> }
        - { arch: arm64, runner: ubuntu-24.04-arm }
  runs-on: ${{ matrix.runner }}
  steps:
    - uses: actions/checkout@v6
    - uses: docker/login-action@v3
      with: { registry: ghcr.io, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
    - run: bash ci/build_sim_images.sh
      env: { ARCH: "${{ matrix.arch }}", IMAGE_PREFIX: ..., IMAGE_TAG: ..., ... }

manifest:
  needs: [tags, build]
  # unchanged: docker buildx imagetools create
```

Accept ~$45/mo (at 300 builds, rising with volume) as the cost of a single, symmetric,
GCP-free build path. If that monthly figure becomes uncomfortable as commits grow,
pursue the "bigger levers" above (arm64 not-every-commit, or self-hosted) — not a
return to the split.
