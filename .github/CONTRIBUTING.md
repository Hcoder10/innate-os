# Contributing

## Building from Source

### Dependencies

All dependencies are managed through config files in `ros2_ws/`:

| File | Description | Usage |
|------|-------------|-------|
| `apt-dependencies.txt` | System & ROS2 apt packages | `xargs sudo apt-get install -y < apt-dependencies.txt` |
| `pip-requirements.txt` | Python packages | `pip3 install -r pip-requirements.txt` |
| `src/dependencies.repos` | External ROS2 repositories | `vcs import src < src/dependencies.repos` |

### Adding Dependencies

- **APT packages**: Add to `ros2_ws/apt-dependencies.{common/hardware}.txt`
- **Python packages**: Add to `ros2_ws/pip-requirements.txt`

These files are used automatically by the local build and CI/CD pipeline.

## Code Style

Formatting and linting (ruff for Python, clang-format for C/C++) are enforced in
CI via [`.github/workflows/format.yml`](workflows/format.yml), so an
unformatted PR will fail the **Format Check** job. To catch issues locally
*before* pushing, install the pre-commit hook so it runs on every `git commit`:

```bash
pip install pre-commit
pre-commit install -c .config/pre-commit-config.yaml
```

The hook versions are pinned in
[`.config/pre-commit-config.yaml`](../.config/pre-commit-config.yaml), so local
and CI results are identical. The `-c` flag is required because the config lives
under `.config/` rather than the repo root. To run the checks manually across
the whole repo:

```bash
pre-commit run --all-files -c .config/pre-commit-config.yaml
```

## Releases

Releases are automatically built via GitHub Actions when a version tag is pushed:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Each release includes:
- `innate-os-{version}.tar.gz` - Full release with pre-built artifacts
- `innate-os-{version}-source.tar.gz` - Source code only
