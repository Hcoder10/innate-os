"""Unit tests for the pure SkillRegistry (no ROS required)."""

from brain_client.skills.registry import SkillRegistry

META = [
    {"id": "innate-os/navigate", "name": "navigate", "guidelines": "g1"},
    {"id": "innate-os/wave", "name": "wave", "guidelines_when_running": "g2"},
]


def test_builds_maps():
    reg = SkillRegistry.from_metadata(META)
    assert set(reg.primitives) == {"innate-os/navigate", "innate-os/wave"}
    assert reg.name_to_id["navigate"] == "innate-os/navigate"
    assert reg.id_to_name["innate-os/wave"] == "wave"


def test_resolve_prefers_id_then_name():
    reg = SkillRegistry.from_metadata(META)
    assert reg.resolve_skill_id("innate-os/navigate") == "innate-os/navigate"  # already an id
    assert reg.resolve_skill_id("navigate") == "innate-os/navigate"  # by name
    assert reg.resolve_skill_id("nonexistent") is None


def test_name_for_falls_back_to_id():
    reg = SkillRegistry.from_metadata(META)
    assert reg.name_for("innate-os/wave") == "wave"
    assert reg.name_for("unknown-id") == "unknown-id"


def test_duplicate_name_callback_and_last_wins():
    seen = []
    meta = META + [{"id": "custom/navigate", "name": "navigate"}]
    reg = SkillRegistry.from_metadata(meta, on_duplicate=lambda n, old, new: seen.append((n, old, new)))
    assert seen == [("navigate", "innate-os/navigate", "custom/navigate")]
    assert reg.name_to_id["navigate"] == "custom/navigate"


def test_stub_guidelines():
    reg = SkillRegistry.from_metadata(META)
    assert reg.primitives["innate-os/navigate"].guidelines() == "g1"
    assert reg.primitives["innate-os/wave"].guidelines_when_running() == "g2"
    assert reg.primitives["innate-os/wave"].guidelines() == ""  # missing key -> empty


def test_empty_registry_is_falsy():
    assert not SkillRegistry()
    assert SkillRegistry.from_metadata(META)
