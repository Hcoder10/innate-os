# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Protocol-level coverage for compact Agent skill activity."""

from types import SimpleNamespace

from brain_client.transport.activity import (
    successful_code_output,
    task_status_history_entry,
    task_status_payload,
)


def result(*, success=True, success_type="success", message="done"):
    return SimpleNamespace(success=success, success_type=success_type, message=message)


def test_successful_code_output_is_preserved_verbatim():
    assert successful_code_output(result(message="  result 1.2345\n"), True) == "  result 1.2345\n"


def test_output_is_omitted_for_non_success_cases():
    assert successful_code_output(result(message="   "), True) is None
    assert successful_code_output(result(success=False, success_type="failure"), True) is None
    assert successful_code_output(result(success_type="cancelled"), True) is None
    assert successful_code_output(result(), False) is None


def test_nested_completion_payload_carries_output_and_omits_blank_output():
    completed = task_status_payload(
        "inspect shelf",
        "child-1",
        "completed",
        skill_id="local/inspect_shelf",
        output="found two items",
        timestamp=12.5,
    )
    assert completed["output"] == "found two items"
    assert completed["timestamp"] == 12.5

    blank = task_status_payload("inspect shelf", "child-2", "completed", output="  ", timestamp=13)
    assert "output" not in blank


def test_status_history_keeps_call_identity_inputs_failure_and_output():
    entry = task_status_history_entry(
        {
            "primitive_name": "inspect shelf",
            "primitive_id": "run-1",
            "skill_id": "local/inspect_shelf",
            "status": "completed",
            "timestamp": 42,
            "args": {"shelf": 2},
            "reason": "exact 192.168.1.10 trace",
            "output": "found two items",
        }
    )
    assert entry == {
        "sender": "task_activated",
        "text": "inspect shelf",
        "timestamp": 42,
        "taskStatus": "completed",
        "primitiveId": "run-1",
        "skillId": "local/inspect_shelf",
        "args": {"shelf": 2},
        "failureReason": "exact 192.168.1.10 trace",
        "output": "found two items",
    }
