"""Tests for the baseline-agent transcript -> ATIF adapter.

Asserts the adapter against a REAL baseline run fixture (mini protocol, curl submission,
both stages), reduced to the two files the adapter reads.
"""

from pathlib import Path

from atif_converter import Trajectory, convert, detect_agent
from atif_converter.adapters import baseline

FIXTURE = Path(__file__).parent / "fixtures" / "baseline_run"
TRANSCRIPT = FIXTURE / "baseline_transcript.jsonl"

# Golden expectations for the committed fixture
EXPECTED_STEPS = 28
EXPECTED_DIAGNOSIS_STEPS = 21
EXPECTED_TOOL_CALLS = 27  # every step but the last of each stage ran a command


def _convert() -> Trajectory:
    return baseline.convert_file(TRANSCRIPT)


def test_to_atif_returns_validated_trajectory():
    traj = _convert()
    assert isinstance(traj, Trajectory)
    assert traj.schema_version == "ATIF-v1.7"
    assert traj.agent.name == "baseline"
    assert traj.agent.model_name == "openai/deepseek-flash"
    assert traj.agent.extra["protocol"] == "mini" and traj.agent.extra["submit_mode"] == "curl"
    assert traj.session_id.startswith("anon_")
    assert len(traj.steps) == EXPECTED_STEPS


def test_every_step_is_one_model_call_with_at_most_one_command():
    traj = _convert()
    assert [step.step_id for step in traj.steps] == list(range(1, EXPECTED_STEPS + 1))
    assert all(step.source == "agent" and step.llm_call_count == 1 for step in traj.steps)
    assert all(len(step.tool_calls) == 1 for step in traj.steps if step.tool_calls)
    with_calls = [step for step in traj.steps if step.tool_calls]
    assert len(with_calls) == EXPECTED_TOOL_CALLS
    for step in with_calls:
        call = step.tool_calls[0]
        assert call.function_name == "bash" and "command" in call.arguments
        result = step.observation.results[0]
        assert result.source_call_id == call.tool_call_id
        assert result.content.startswith("<returncode>")


def test_stages_and_submissions_are_recorded():
    traj = _convert()
    stages = traj.extra["baseline"]["stages"]
    assert [stage["stage"] for stage in stages] == ["diagnosis", "mitigation"]
    assert stages[0]["steps"] == EXPECTED_DIAGNOSIS_STEPS
    assert all(stage["reason"] == "submitted_by_command" for stage in stages)
    assert traj.extra["baseline"]["submissions"] == [
        {"stage": "diagnosis", "by": "command"},
        {"stage": "mitigation", "by": "command"},
    ]
    assert traj.extra["baseline"]["return_code"] == 0
    assert {step.extra["stage"] for step in traj.steps} == {"diagnosis", "mitigation"}


def test_reasoning_and_token_metrics_are_carried_over():
    traj = _convert()
    assert all(step.reasoning_content for step in traj.steps)
    assert all(step.reasoning_effort == "high" for step in traj.steps)
    first = traj.steps[0].metrics
    assert first.prompt_tokens == 760 and first.completion_tokens == 74 and first.cached_tokens == 512
    assert first.extra["token_metrics_version"] == 2 and first.extra["reasoning_output_tokens"] == 33
    final = traj.final_metrics
    assert final.total_steps == EXPECTED_STEPS
    assert final.total_prompt_tokens == sum(step.metrics.prompt_tokens for step in traj.steps)
    assert final.total_cached_tokens == sum(step.metrics.cached_tokens for step in traj.steps)
    assert final.extra["token_metrics_version"] == 2


def test_transcript_without_a_run_directory_still_converts():
    records = [line for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines() if line.strip()]
    import json

    traj = baseline.convert_records([json.loads(line) for line in records])
    assert len(traj.steps) == EXPECTED_STEPS
    assert all(step.reasoning_content is None for step in traj.steps)


def test_the_format_is_detected_and_dispatched():
    assert detect_agent(TRANSCRIPT) == "baseline"
    assert convert(TRANSCRIPT).agent.name == "baseline"
    assert convert(TRANSCRIPT, agent="baseline").agent.name == "baseline"
