import json

import pytest

from clients.baseline import protocol
from clients.codex.driver import build_instruction

APP = {"app_name": "Hotel Reservation", "namespace": "hotel-reservation", "descriptions": "A hotel app."}


def test_task_text_is_the_codex_instruction_verbatim():
    assert protocol.build_task_text(APP) == build_instruction(APP)


def test_zero_budget_prompt_has_no_transcript_and_only_allows_submit():
    prompt = protocol.build_step_prompt(
        protocol.build_task_text(APP), mode="full", max_commands=0, used=0, steps=[], submit_only=True
    )
    assert prompt.startswith("[TASK]\n")
    assert "[TRANSCRIPT]" not in prompt
    assert "cannot run any commands" in prompt
    assert 'Only "submit" is accepted' in prompt
    assert prompt.rstrip().endswith("Reply with the JSON object for step 1.")


def test_budget_lines_and_violation_notice():
    assert "at most 3 commands" in protocol.budget_line(3, 1)
    assert "Remaining: 2" in protocol.budget_line(3, 1)
    assert "no fixed command budget" in protocol.budget_line(None, 7)
    prompt = protocol.build_step_prompt(
        "task", mode="no_mechanism", max_commands=3, used=3, steps=[], submit_only=True, violation=["because"]
    )
    assert "[NOTICE]" in prompt and "'because'" in prompt


def test_schema_shapes_are_strict_mode_friendly():
    full = protocol.step_schema("full", False)
    assert full["properties"]["action"]["enum"] == ["command", "submit"]
    assert full["required"] == ["action", "note", "command", "diagnosis"]
    assert full["additionalProperties"] is False
    assert protocol.step_schema("full", True)["properties"]["action"]["enum"] == ["submit"]
    nomech = protocol.step_schema("no_mechanism", False)
    assert "diagnosis" not in nomech["properties"]
    assert {"faulty_component", "affected_components", "symptom"} <= set(nomech["properties"])
    assert set(nomech["required"]) == set(nomech["properties"])
    json.dumps(nomech)


def test_validate_step():
    ok_command = {"action": "command", "note": "n", "command": "kubectl get pods", "diagnosis": None}
    assert protocol.validate_step(ok_command, "full", False) is None
    assert "action" in protocol.validate_step(ok_command, "full", True)
    assert "diagnosis" in protocol.validate_step(
        {"action": "submit", "note": "n", "command": None, "diagnosis": ""}, "full", False
    )
    assert "missing keys" in protocol.validate_step({"action": "submit"}, "full", False)
    assert "unexpected keys" in protocol.validate_step({**ok_command, "extra": 1}, "full", False)
    assert protocol.validate_step("nope", "full", False) == "reply is not a JSON object"
    ok_nomech = {
        "action": "submit",
        "note": "n",
        "command": None,
        "faulty_component": "deployment/x",
        "affected_components": ["frontend"],
        "symptom": "500s",
    }
    assert protocol.validate_step(ok_nomech, "no_mechanism", True) is None
    assert "affected_components" in protocol.validate_step(
        {**ok_nomech, "affected_components": "frontend"}, "no_mechanism", True
    )


def test_compose_submission_and_mechanism_guard():
    parsed = {
        "action": "submit",
        "note": "n",
        "command": None,
        "faulty_component": "service/frontend",
        "affected_components": ["search"],
        "symptom": "requests to the frontend fail intermittently",
    }
    assert protocol.mechanism_guard_hits(parsed, "no_mechanism") == []
    assert protocol.compose_submission(parsed, "no_mechanism") == (
        "Faulty component: service/frontend. Affected components: search. "
        "Observed symptom: requests to the frontend fail intermittently."
    )
    leaky = {**parsed, "symptom": "fails because the selector is wrong"}
    assert protocol.mechanism_guard_hits(leaky, "no_mechanism") == ["because", "wrong"]
    assert protocol.mechanism_guard_hits(leaky, "full") == []
    assert protocol.compose_submission({"diagnosis": "  text  "}, "full") == "text"
    assert "none identified" in protocol.compose_submission({**parsed, "affected_components": None}, "no_mechanism")


def test_truncate_keeps_head_and_tail():
    text = "a" * 100 + "b" * 100
    out = protocol.truncate(text, 40)
    assert out.startswith("a" * 30)
    assert out.endswith("b" * 10)
    assert "[... truncated 160 chars ...]" in out
    assert protocol.truncate("short", 40) == "short"


def test_transcript_shrinks_old_steps_but_not_recent_ones():
    steps = [protocol.StepRecord(i, f"cmd{i}", 0, "x" * 5000, "") for i in range(1, 8)]
    text = protocol.render_transcript(steps, output_chars=8000, recent=5, old_chars=1500)
    assert text.startswith("[TRANSCRIPT]\nStep 1 - command: cmd1")
    assert "truncated" in text.split("Step 2 -")[0]
    assert "truncated" not in text.split("Step 7 -")[1]
    refused = protocol.StepRecord(8, "curl /submit", 126, "", "no", refused="external_submit")
    assert "refused: external_submit" in protocol.render_step(refused, 1000)


def test_mitigation_stage_prompt_and_submission():
    prompt = protocol.build_step_prompt(
        "task", mode="full", max_commands=None, used=0, steps=[], submit_only=False, stage="mitigation"
    )
    assert "[MITIGATION STAGE]" in prompt and "Current stage: mitigation" in prompt
    assert "[SUBMISSION FORMAT]" not in prompt
    done = {"action": "submit", "note": "fixed", "command": None, "diagnosis": None}
    assert protocol.validate_step(done, "full", False, "mitigation") is None
    assert protocol.validate_step(done, "full", False, "diagnosis") is not None
    assert protocol.compose_submission(done, "full", "mitigation") == ""


def test_session_turn_layout():
    step = protocol.StepRecord(1, "kubectl get pods", 0, "pod-a", "", 0.1)
    turn = protocol.build_session_turn(step, max_commands=3, used=1, next_step=2, submit_only=False)
    assert turn.startswith("[RESULT]\nStep 1 - command: kubectl get pods\n")
    assert "[STATUS]\nCommand budget: you may run at most 3 commands in this run. Used: 1. Remaining: 2." in turn
    assert turn.rstrip().endswith("[NOW]\nReply with the JSON object for step 2.")
    forced = protocol.build_session_turn(
        None, max_commands=None, used=4, next_step=5, submit_only=True, violation=["because"], unusable_reply="bad"
    )
    assert "[RESULT]" not in forced and 'Only "submit" is accepted at this step.' in forced
    assert "[NOTICE]\nYour previous reply could not be used: bad." in forced and "'because'" in forced
    change = protocol.stage_change_text("mitigation")
    assert change.startswith("[STAGE CHANGE]\nThe diagnosis stage is over. Current stage: mitigation.")
    assert "[MITIGATION STAGE]" in change
    with pytest.raises(ValueError):
        protocol.stage_change_text("nope")
