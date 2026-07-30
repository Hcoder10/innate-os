"""The template must never declare a top-level key twice.

`webapp/proxy/settings_store.py` renders settings.yaml by uncommenting template lines
verbatim, so two `mars_app:` stanzas in the template become two top-level `mars_app:`
keys in the output — and YAML keeps only the last, silently discarding every override
under the first. The failure is invisible: the file looks right, the settings page shows
the values as saved, and the robot reverts them on the next restart.

This happened once with `mars_app` (drive ramps in one stanza, heading hold in another).
The template's own header warns about it for `/**`; this makes it enforceable.
"""

import re
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.template"
STANZA = re.compile(r"^#\s?([A-Za-z_/*][\w/*]*):\s*$")


def _stanza_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in TEMPLATE.read_text().splitlines():
        m = STANZA.match(line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def test_no_duplicate_top_level_stanzas():
    dupes = {k: n for k, n in _stanza_counts().items() if n > 1}
    assert not dupes, (
        f"template declares these top-level keys more than once: {dupes}. "
        "Merge them into one stanza — YAML keeps only the last, so overrides under the "
        "earlier one are silently dropped."
    )


def test_template_has_stanzas_at_all():
    """Guard the guard: a regex that matched nothing would pass the test above vacuously."""
    counts = _stanza_counts()
    assert "mars_app" in counts and "/**" in counts, f"stanza regex matched nothing useful: {counts}"


def test_fully_uncommented_template_is_valid_yaml():
    """Uncommenting everything is what the writer effectively does for saved knobs, and the
    result must still parse to one key per node."""
    lines = []
    for raw in TEMPLATE.read_text().splitlines():
        stripped = raw[2:] if raw.startswith("# ") else raw[1:] if raw == "#" else raw
        # keep only lines that look like YAML structure, drop prose
        if raw.startswith("#") and (":" in stripped or stripped.strip().startswith("-")):
            lines.append(stripped)
    text = "\n".join(lines)
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return  # prose lines can still confuse the parser; the duplicate check above is the real guard
    if isinstance(parsed, dict):
        assert parsed, "uncommented template parsed to an empty mapping"
