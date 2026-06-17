"""Registry integrity + overrides-template sync (ROS-free, no live nodes).

Guards the common ways the central-config registry drifts:
  * a malformed, duplicate, or mistyped entry,
  * a ``default`` that violates its own type/bounds,
  * the committed ``overrides.yaml`` falling out of sync with the registry,
  * a broken/unknown line shipped in ``overrides.yaml``.

Scope note: confirming that each ``node``/``param`` matches a parameter the
running node actually declares needs the nodes live — that is a heavier
launch-test left as a follow-up. This file is the fast, non-flaky unit layer.
"""

import os
import sys
from pathlib import Path

# Resolve the registry from this repo's source tree, hermetically.
_PKG_ROOT = Path(__file__).resolve().parents[1]   # ros2_ws/src/innate_config
_REPO_ROOT = Path(__file__).resolve().parents[4]  # <innate-os>
os.environ["INNATE_OS_ROOT"] = str(_REPO_ROOT)
sys.path.insert(0, str(_PKG_ROOT))

import yaml  # noqa: E402
from innate_config import registry as r  # noqa: E402

_VALID_TYPES = {"float", "int", "bool", "str", "enum"}
_REQUIRED = {"name", "group", "node", "param", "type", "desc"}


def _entries():
    return r.load_registry()


def test_entries_well_formed():
    seen = set()
    for e in _entries():
        name = e.get("name", e)
        assert not (_REQUIRED - e.keys()), f"{name}: missing fields {_REQUIRED - e.keys()}"
        assert e["type"] in _VALID_TYPES, f"{name}: invalid type {e['type']!r}"
        assert e["name"] not in seen, f"duplicate knob name {e['name']}"
        seen.add(e["name"])
        assert str(e["node"]).startswith("/"), f"{name}: node must be fully-qualified (got {e['node']!r})"
        if e["type"] in ("int", "float"):
            lo, hi = e.get("min"), e.get("max")
            if lo is not None and hi is not None:
                assert lo <= hi, f"{name}: min {lo} > max {hi}"
        if e["type"] == "enum":
            assert e.get("choices"), f"{name}: enum knob needs choices"


def test_defaults_pass_their_own_validation():
    # A default that violates its declared type/bounds/choices is a bug: the
    # reference value shown to users would be one the CLI/web app would reject.
    for e in _entries():
        if "default" in e:
            r.coerce(e, e["default"])  # raises ConfigError on violation


def test_committed_overrides_lists_every_knob():
    # Drift guard: adding a knob to the registry without regenerating
    # overrides.yaml would hide it from users. Every name must appear in the
    # committed file (as a commented or active line).
    text = r.overrides_path().read_text()
    for e in _entries():
        assert f"{e['name']}:" in text, (
            f"{e['name']} is missing from overrides.yaml — "
            f"regenerate it from the registry (render_template)."
        )


def test_committed_overrides_has_no_broken_lines():
    # Whatever is currently uncommented in overrides.yaml must translate without
    # warnings (no unknown names, no out-of-range/typo'd values).
    _node_params, warnings = r.build_node_params()
    assert warnings == [], f"overrides.yaml has invalid lines: {warnings}"


def test_rendered_template_round_trips():
    text = r.render_template()
    parsed = yaml.safe_load(text)  # must be valid YAML
    # all-commented render parses to nothing (no active overrides)
    assert parsed is None or parsed == {}, parsed
    for e in _entries():
        assert f"{e['name']}:" in text
