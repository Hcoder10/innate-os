"""Unit tests for the skill invoker (``self.skills.run(...)``) and its app-facing
substep lifecycle markers.

Covers: code- and physical-child dispatch, result/status passthrough, the
running/completed/failed/interrupted feedback markers each child emits, unknown
skills, and cancellation short-circuiting the rest of a routine. Uses fakes for
the skills server, so no ROS runtime is needed beyond importing the modules.
"""

from brain_client.skills.invoker import SkillInvoker
from brain_client.skills.lifecycle import decode_substep_feedback, encode_substep_feedback
from brain_client.skills.types import SkillResult


class _Logger:
    def info(self, *a, **k):
        pass

    error = warn = debug = info


class _Catalog:
    def __init__(self, code=None, physical=None):
        self._code = code or {}
        self._physical = physical or {}

    def get_code_skill(self, skill_id):
        return self._code.get(skill_id)

    def get_physical_skill(self, skill_id):
        return self._physical.get(skill_id)


class _Server:
    """Minimal stand-in for SkillsActionServer's invoker-facing surface."""

    def __init__(self, catalog, physical_result=None):
        self.catalog = catalog
        self._logger = _Logger()
        self._physical_result = physical_result or (True, "ok", SkillResult.SUCCESS.value, "succeed")
        self.behavior_cancels = []

    def get_logger(self):
        return self._logger

    def _run_code_skill_body(self, skill, skill_id, inputs):
        return skill.execute(**inputs)

    def _run_physical_skill(self, goal_handle, skill_id, physical_data):
        return self._physical_result

    def _request_behavior_goal_cancel(self, goal_handle, skill_type):
        self.behavior_cancels.append(skill_type)


class _CodeSkill:
    def __init__(self, name, result):
        self.name = name
        self._result = result
        self.cancelled = False

    def set_feedback_callback(self, cb):
        self._cb = cb

    def execute(self, **inputs):
        self.last_inputs = inputs
        return self._result

    def cancel(self):
        self.cancelled = True
        return "cancelled"


def _events(feedbacks):
    return [decode_substep_feedback(f)["event"] for f in feedbacks]


def test_lifecycle_marker_roundtrip():
    encoded = encode_substep_feedback(
        event="completed", name="wave", primitive_id="abc", skill_id="innate-os/wave", output="done"
    )
    decoded = decode_substep_feedback(encoded)
    assert decoded == {
        "event": "completed",
        "name": "wave",
        "primitive_id": "abc",
        "skill_id": "innate-os/wave",
        "output": "done",
    }
    assert decode_substep_feedback("just normal feedback") is None
    assert decode_substep_feedback("") is None


def test_run_code_child_emits_steps_and_passes_inputs():
    feedbacks = []
    skill = _CodeSkill("nav", ("arrived", SkillResult.SUCCESS))
    server = _Server(_Catalog(code={"innate-os/nav": ("nav", skill)}))
    invoker = SkillInvoker(server, goal_handle=object(), publish_feedback=feedbacks.append)

    message, status = invoker.run("innate-os/nav", x=1.0, y=2.0)

    assert (message, status) == ("arrived", SkillResult.SUCCESS)
    assert skill.last_inputs == {"x": 1.0, "y": 2.0}
    assert _events(feedbacks) == ["running", "completed"]


def test_run_physical_child_emits_steps():
    feedbacks = []
    server = _Server(_Catalog(physical={"local/policy": {"metadata": {"name": "policy"}, "directory": "/x"}}))
    invoker = SkillInvoker(server, object(), feedbacks.append)

    message, status = invoker.run("local/policy")

    assert status is SkillResult.SUCCESS
    assert _events(feedbacks) == ["running", "completed"]


def test_failed_child_emits_failed_marker():
    feedbacks = []
    skill = _CodeSkill("grab", ("no object", SkillResult.FAILURE))
    server = _Server(_Catalog(code={"innate-os/grab": ("grab", skill)}))
    invoker = SkillInvoker(server, object(), feedbacks.append)

    _message, status = invoker.run("innate-os/grab")

    assert status is SkillResult.FAILURE
    assert _events(feedbacks) == ["running", "failed"]
    assert decode_substep_feedback(feedbacks[-1])["reason"] == "no object"


def test_unknown_skill_fails_without_marker():
    feedbacks = []
    server = _Server(_Catalog())
    invoker = SkillInvoker(server, object(), feedbacks.append)

    message, status = invoker.run("nope")

    assert status is SkillResult.FAILURE
    assert "Unknown skill" in message
    assert feedbacks == []


def test_cancel_short_circuits_further_runs():
    feedbacks = []
    skill = _CodeSkill("nav", ("arrived", SkillResult.SUCCESS))
    server = _Server(_Catalog(code={"innate-os/nav": ("nav", skill)}))
    invoker = SkillInvoker(server, object(), feedbacks.append)

    invoker.cancel()
    message, status = invoker.run("innate-os/nav")

    assert status is SkillResult.CANCELLED
    assert feedbacks == []  # short-circuited before running anything


def test_cancelled_child_marks_routine_cancelled():
    skill = _CodeSkill("nav", ("stopped", SkillResult.CANCELLED))
    server = _Server(_Catalog(code={"innate-os/nav": ("nav", skill)}))
    invoker = SkillInvoker(server, object(), lambda *_: None)

    _m1, s1 = invoker.run("innate-os/nav")
    _m2, s2 = invoker.run("innate-os/nav")

    assert s1 is SkillResult.CANCELLED
    assert s2 is SkillResult.CANCELLED  # chain stays cancelled after a cancelled child
