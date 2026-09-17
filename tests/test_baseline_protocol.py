import pytest

from clients.baseline import protocol
from clients.codex.driver import build_instruction

APP = {"app_name": "Hotel Reservation", "namespace": "hotel-reservation", "descriptions": "A hotel app."}


def test_task_text_is_the_codex_instruction_verbatim():
    assert protocol.build_task_text(APP) == build_instruction(APP)


def test_opening_turn_and_budget_lines():
    turn = protocol.opening_turn("TASK", stage="diagnosis", max_calls=3)
    assert turn.startswith("[TASK]\nTASK\n\n[PROTOCOL]\n")
    assert "at most 3 tool calls in this stage (submit does not count). Used: 0. Remaining: 3." in turn
    assert "Current stage: diagnosis. Call submit when you have your diagnosis." in turn
    assert "cannot call any tool other than submit" in protocol.budget_line(0, 0)
    assert "no fixed tool-call budget" in protocol.budget_line(None, 7)
    with pytest.raises(ValueError):
        protocol.opening_turn("TASK", stage="nope", max_calls=None)


def test_stage_change_forced_and_notice_texts():
    change = protocol.stage_change_turn(None)
    assert change.startswith("[STAGE CHANGE]\nThe diagnosis has been submitted. Current stage: mitigation.")
    assert "empty mitigation submission" in change and "no fixed tool-call budget" in change
    forced = protocol.forced_submit_turn("hard cap")
    assert "[NOTICE]" in forced and "(hard cap)" in forced and "Only the submit tool is available" in forced
    assert "no tool call" in protocol.NUDGE_TEXT
    assert "could not be used: bad." in protocol.UNUSABLE_REPLY_TEXT.format(error="bad")


def test_system_text_carries_the_submission_rules():
    assert "which component is at fault, what is wrong with it, why" in protocol.system_text("full")
    assert "Do NOT explain why it fails" in protocol.system_text("no_mechanism")
    with pytest.raises(ValueError):
        protocol.system_text("nope")


def test_submit_tool_shapes():
    full = protocol.submit_tool_spec("full", "diagnosis").as_openai()["function"]
    assert full["name"] == "submit" and full["parameters"]["required"] == ["diagnosis"]
    nomech = protocol.submit_tool_spec("no_mechanism", "diagnosis").as_openai()["function"]["parameters"]
    assert nomech["required"] == ["faulty_component", "affected_components", "symptom"]
    assert nomech["properties"]["affected_components"]["type"] == "array"
    mitigation = protocol.submit_tool_spec("full", "mitigation").as_openai()["function"]
    assert "required" not in mitigation["parameters"] and "note" in mitigation["parameters"]["properties"]


def test_validate_and_compose_submission():
    assert protocol.validate_submit({"diagnosis": "x"}, "full", "diagnosis") is None
    assert "diagnosis" in protocol.validate_submit({"diagnosis": ""}, "full", "diagnosis")
    assert "not an object" in protocol.validate_submit("nope", "full", "diagnosis")
    assert protocol.validate_submit({}, "full", "mitigation") is None
    args = {
        "faulty_component": "service/frontend",
        "affected_components": ["search"],
        "symptom": "requests fail intermittently",
    }
    assert protocol.validate_submit(args, "no_mechanism", "diagnosis") is None
    assert "affected_components" in protocol.validate_submit(
        {**args, "affected_components": "search"}, "no_mechanism", "diagnosis"
    )
    assert protocol.compose_submission(args, "no_mechanism", "diagnosis") == (
        "Faulty component: service/frontend. Affected components: search. Observed symptom: requests fail intermittently."
    )
    assert "none identified" in protocol.compose_submission(
        {**args, "affected_components": None}, "no_mechanism", "diagnosis"
    )
    assert protocol.compose_submission({"diagnosis": "  text  "}, "full", "diagnosis") == "text"
    assert protocol.compose_submission({"note": "fixed"}, "full", "mitigation") == ""


def test_mechanism_guard():
    args = {
        "faulty_component": "service/frontend",
        "affected_components": ["search"],
        "symptom": "fails because the selector is wrong",
    }
    assert protocol.mechanism_guard_hits(args, "no_mechanism") == ["because", "wrong"]
    assert protocol.mechanism_guard_hits(args, "full") == []
    assert "'because'" in protocol.GUARD_REJECTION_TEXT.format(hits="'because'")


def test_truncate_keeps_head_and_tail():
    text = "a" * 100 + "b" * 100
    out = protocol.truncate(text, 40)
    assert out.startswith("a" * 30) and out.endswith("b" * 10) and "[... truncated 160 chars ...]" in out
    assert protocol.truncate("short", 40) == "short"
