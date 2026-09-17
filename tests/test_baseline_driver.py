import json
import sys
from pathlib import Path

import pytest

from clients.baseline import driver, protocol
from clients.baseline.backends import CodexExecBackend, StepResult, parse_codex_usage
from clients.harness.token_usage import usage_metrics
from sregym.run_artifacts import _find_token_hit

APP = {"app_name": "Hotel Reservation", "namespace": "hotel-reservation", "descriptions": "A hotel app."}
REAL_PROBLEM_ID = "service_wrong_pod_selection_hotel_reservation"
DIAGNOSIS = "The frontend Service selector also matches the search pod."


class FakeBackend:
    name = "fake"
    model = "fake-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    def version(self):
        return "fake-1"

    def complete_json(self, prompt, schema, *, step_dir):
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "prompt.txt").write_text(prompt)
        (step_dir / "schema.json").write_text(json.dumps(schema))
        self.calls.append((prompt, schema))
        reply = self.replies.pop(0)
        if isinstance(reply, StepResult):
            return reply
        usage = usage_metrics(input_tokens=100, output_tokens=10, cached_input_tokens=50, reasoning_output_tokens=5)
        return StepResult(parsed=reply, raw_text=json.dumps(reply), usage=usage, latency_s=0.1)


class FakeSessionBackend(FakeBackend):
    """A backend that accepts the driver's message list, like ApiBackend does in session mode."""

    def __init__(self, replies):
        super().__init__(replies)
        self.sessions: list[list[dict]] = []

    def system_message(self, schema):
        return {"role": "system", "content": "JSON only. " + json.dumps(schema)}

    def complete_messages(self, messages, *, step_dir):
        import copy

        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(json.dumps(messages))
        self.sessions.append(copy.deepcopy(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, StepResult):
            return reply
        usage = usage_metrics(input_tokens=100, output_tokens=10, cached_input_tokens=50, reasoning_output_tokens=5)
        return StepResult(
            parsed=reply,
            raw_text=json.dumps(reply),
            usage=usage,
            latency_s=0.1,
            reasoning=f"thinking {len(self.sessions)}",
        )


def command(cmd):
    return {"action": "command", "note": "look", "command": cmd, "diagnosis": None}


def submit(text):
    return {"action": "submit", "note": "done", "command": None, "diagnosis": text}


def submit_nomech(component, affected, symptom):
    return {
        "action": "submit",
        "note": "done",
        "command": None,
        "faulty_component": component,
        "affected_components": affected,
        "symptom": symptom,
    }


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = {"stages": ["diagnosis", "done"], "submissions": [], "commands": [], "logs": tmp_path, "current": None}
    monkeypatch.setattr(driver, "AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(driver, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(driver, "resolve_problem_id", lambda cli_problem_id=None: "anon_test")
    monkeypatch.setattr(driver, "get_app_info", lambda: APP)
    monkeypatch.setattr(driver, "current_stage", lambda: state["current"])

    def wait_for_stage(stages, timeout):
        state["current"] = state["stages"].pop(0)
        return state["current"]

    monkeypatch.setattr(driver, "wait_for_stage", wait_for_stage)

    def submit_to_conductor(solution, stage):
        state["submissions"].append((solution, stage))
        return {"status": "200", "message": "Submission received", "stage": stage}

    monkeypatch.setattr(driver, "submit_to_conductor", submit_to_conductor)

    def run_command(cmd, timeout, cwd=None):
        state["commands"].append(cmd)
        return driver.CommandResult("pod-a Running\n", "", 0, 0.01)

    monkeypatch.setattr(driver, "run_command", run_command)
    monkeypatch.setattr(sys, "argv", ["driver"])
    return state


def run_main(monkeypatch, backend, *, max_commands="10", mode="full", hard_cap=60, context="stateless"):
    monkeypatch.setattr(driver, "MAX_COMMANDS_RAW", max_commands)
    monkeypatch.setattr(driver, "SUBMISSION_MODE", mode)
    monkeypatch.setattr(driver, "HARD_CAP", hard_cap)
    monkeypatch.setattr(driver, "CONTEXT", context)
    monkeypatch.setattr(driver, "make_backend", lambda name, model, effort: backend)
    with pytest.raises(SystemExit) as exc:
        driver.main()
    return exc.value.code


def read_results(logs: Path) -> dict:
    files = list(logs.glob("baseline_results_*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def read_transcript(logs: Path) -> list[dict]:
    return [json.loads(line) for line in (logs / "baseline_transcript.jsonl").read_text().splitlines()]


def test_zero_budget_makes_one_submit_only_call(monkeypatch, harness):
    backend = FakeBackend([submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="0") == 0
    assert len(backend.calls) == 1
    prompt, schema = backend.calls[0]
    assert schema["properties"]["action"]["enum"] == ["submit"]
    assert "[TRANSCRIPT]" not in prompt
    assert harness["commands"] == []
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]
    results = read_results(harness["logs"])
    assert results["problem_id"] == "anon_test"
    assert results["success"] is True
    assert results["baseline"]["commands_used"] == 0
    assert results["baseline"]["model_calls"] == 1
    assert results["baseline"]["termination_reason"] == "budget_exhausted_forced_submit"
    assert results["usage_metrics"]["input_tokens"] == 100
    assert results["usage_metrics"]["token_metrics_version"] == 2
    records = read_transcript(harness["logs"])
    kinds = [record["type"] for record in records]
    assert kinds[:2] == ["meta", "model_call"] and "submit" in kinds and kinds[-1] == "end"
    assert records[1]["repaired"] is False


def test_budget_counts_refused_commands_and_forces_submit(monkeypatch, harness):
    backend = FakeBackend(
        [
            command("kubectl get pods -n hotel-reservation"),
            command("curl -X POST http://host.docker.internal:8000/submit -d '{}'"),
            command("kubectl logs deploy/frontend"),
            submit(DIAGNOSIS),
        ]
    )
    assert run_main(monkeypatch, backend, max_commands="3") == 0
    assert harness["commands"] == ["kubectl get pods -n hotel-reservation", "kubectl logs deploy/frontend"]
    fourth_prompt, fourth_schema = backend.calls[3]
    assert fourth_schema["properties"]["action"]["enum"] == ["submit"]
    assert "refused: external_submit" in fourth_prompt
    assert "Remaining: 0" in fourth_prompt
    results = read_results(harness["logs"])
    assert results["baseline"]["commands_used"] == 3
    assert results["baseline"]["termination_reason"] == "budget_exhausted_forced_submit"
    assert results["usage_metrics"]["input_tokens"] == 400
    assert _find_token_hit(harness["logs"], REAL_PROBLEM_ID) is None


def test_unlimited_budget_is_bounded_by_the_hard_cap(monkeypatch, harness):
    backend = FakeBackend([command("a"), command("b"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="unlimited", hard_cap=2) == 0
    assert backend.calls[2][1]["properties"]["action"]["enum"] == ["submit"]
    assert "no fixed command budget" in backend.calls[1][0]
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "hard_cap_forced_submit"


def test_voluntary_submit_keeps_its_own_reason(monkeypatch, harness):
    backend = FakeBackend([command("kubectl get pods"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="10") == 0
    results = read_results(harness["logs"])
    assert results["baseline"]["termination_reason"] == "submitted"
    assert results["baseline"]["commands_used"] == 1


def test_no_mechanism_mode_composes_text_and_reasks_once(monkeypatch, harness):
    backend = FakeBackend(
        [
            submit_nomech("service/frontend", ["search"], "fails because the selector is wrong"),
            submit_nomech("service/frontend", ["search"], "requests to the frontend fail"),
        ]
    )
    assert run_main(monkeypatch, backend, max_commands="0", mode="no_mechanism") == 0
    assert "diagnosis" not in backend.calls[0][1]["properties"]
    assert "[NOTICE]" in backend.calls[1][0]
    assert harness["submissions"] == [
        (
            "Faulty component: service/frontend. Affected components: search. Observed symptom: requests to the frontend fail.",
            "diagnosis",
        )
    ]
    assert read_results(harness["logs"])["baseline"]["mechanism_guard_tripped"] is False


def test_no_mechanism_mode_submits_with_flag_after_second_violation(monkeypatch, harness):
    leaky = submit_nomech("service/frontend", ["search"], "fails because the selector is wrong")
    backend = FakeBackend([leaky, leaky])
    assert run_main(monkeypatch, backend, max_commands="0", mode="no_mechanism") == 0
    assert len(backend.calls) == 2
    assert read_results(harness["logs"])["baseline"]["mechanism_guard_tripped"] is True


def test_two_bad_replies_submit_the_fallback_and_exit_2(monkeypatch, harness):
    backend = FakeBackend(
        [
            StepResult(None, "garbage", error="reply is not valid JSON"),
            StepResult({"action": "dance"}, '{"action": "dance"}'),
        ]
    )
    assert run_main(monkeypatch, backend, max_commands="3") == 2
    assert harness["submissions"] == [(driver.FALLBACK_DIAGNOSIS, "diagnosis")]
    results = read_results(harness["logs"])
    assert results["success"] is False
    assert results["baseline"]["termination_reason"] == "model_failure"


def test_mitigation_stage_runs_the_loop_and_sends_the_empty_submission(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend(
        [
            submit(DIAGNOSIS),
            command("kubectl patch svc frontend -n hotel-reservation -p '{}'"),
            {"action": "submit", "note": "fixed", "command": None, "diagnosis": None},
        ]
    )
    assert run_main(monkeypatch, backend, max_commands="10") == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis"), ("", "mitigation")]
    assert harness["commands"] == ["kubectl patch svc frontend -n hotel-reservation -p '{}'"]
    mitigation_prompt = backend.calls[1][0]
    assert "[MITIGATION STAGE]" in mitigation_prompt and "Current stage: mitigation" in mitigation_prompt
    results = read_results(harness["logs"])
    assert results["baseline"]["submitted_stages"] == ["diagnosis", "mitigation"]
    assert results["baseline"]["stages"]["diagnosis"]["commands_used"] == 0
    assert results["baseline"]["stages"]["mitigation"]["commands_used"] == 1
    assert results["baseline"]["stages"]["mitigation"]["termination_reason"] == "submitted"
    assert results["usage_metrics"]["input_tokens"] == 300
    assert (harness["logs"] / "steps" / "step_03").exists()


def test_mitigation_transcript_carries_the_diagnosis_steps(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend([command("kubectl get pods"), submit(DIAGNOSIS), submit("")])
    assert run_main(monkeypatch, backend, max_commands="10") == 0
    mitigation_prompt = backend.calls[2][0]
    assert "Step 1 - command: kubectl get pods" in mitigation_prompt
    assert "Reply with the JSON object for step 2." in mitigation_prompt
    assert read_results(harness["logs"])["baseline"]["stages"]["mitigation"]["commands_used"] == 0


def test_attempt_starting_at_mitigation_runs_only_the_mitigation_loop(monkeypatch, harness):
    harness["stages"] = ["mitigation", "done"]
    backend = FakeBackend([submit("")])
    assert run_main(monkeypatch, backend, max_commands="3") == 0
    assert len(backend.calls) == 1
    assert harness["submissions"] == [("", "mitigation")]
    assert "diagnosis" not in read_results(harness["logs"])["baseline"]["stages"]


def test_external_submission_is_detected_and_not_resubmitted(monkeypatch, harness):
    monkeypatch.setattr(driver, "current_stage", lambda: "tearing_down")
    backend = FakeBackend([command("python3 -c 'import requests'"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="3") == 3
    assert harness["submissions"] == []
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "external_submission"


def test_session_mode_keeps_one_growing_conversation(monkeypatch, harness):
    backend = FakeSessionBackend([command("kubectl get pods"), command("kubectl get svc"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="10", context="session") == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]
    first, second, third = backend.sessions
    assert [m["role"] for m in first] == ["system", "user"]
    assert first[1]["content"] == protocol.build_step_prompt(
        protocol.build_task_text(APP), mode="full", max_commands=10, used=0, steps=[], submit_only=False
    )
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[:2] == first  # the prefix never changes, so the provider can cache it
    assert second[2] == {
        "role": "assistant",
        "content": json.dumps(command("kubectl get pods")),
        "reasoning_content": "thinking 1",
    }
    assert "[RESULT]\nStep 1 - command: kubectl get pods" in second[3]["content"]
    assert "pod-a Running" in second[3]["content"]
    assert second[3]["content"].rstrip().endswith("Reply with the JSON object for step 2.")
    assert "[TRANSCRIPT]" not in second[3]["content"]
    assert len(third) == 6 and third[:4] == second
    assert "Step 2 - command: kubectl get svc" in third[5]["content"]
    results = read_results(harness["logs"])
    assert results["baseline"]["context"] == "session" and results["baseline"]["session_reasoning"] is True
    assert results["baseline"]["commands_used"] == 2 and results["baseline"]["termination_reason"] == "submitted"
    calls = [r for r in read_transcript(harness["logs"]) if r["type"] == "model_call"]
    assert [c["context"] for c in calls] == ["session"] * 3 and [c["messages"] for c in calls] == [3, 5, 7]
    assert (harness["logs"] / "steps" / "step_03" / "messages.json").exists()


def test_session_mode_submit_only_goes_into_the_turn_not_the_system_message(monkeypatch, harness):
    backend = FakeSessionBackend([command("kubectl get pods"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="1", context="session") == 0
    first, second = backend.sessions
    assert second[0] == first[0]
    assert '"enum": ["command", "submit"]' in second[0]["content"]
    assert 'Only "submit" is accepted at this step.' in second[3]["content"]
    assert "Used: 1. Remaining: 0." in second[3]["content"]
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "budget_exhausted_forced_submit"


def test_session_mode_continues_into_mitigation(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeSessionBackend(
        [
            command("kubectl get pods"),
            submit(DIAGNOSIS),
            command("kubectl patch deploy/frontend -p '{}'"),
            {"action": "submit", "note": "fixed", "command": None, "diagnosis": None},
        ]
    )
    assert run_main(monkeypatch, backend, max_commands="10", context="session") == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis"), ("", "mitigation")]
    diagnosis_end, mitigation_start, mitigation_end = backend.sessions[1], backend.sessions[2], backend.sessions[3]
    assert mitigation_start[: len(diagnosis_end)] == diagnosis_end
    assert mitigation_start[-2]["content"] == json.dumps(submit(DIAGNOSIS))
    turn = mitigation_start[-1]["content"]
    assert "[STAGE CHANGE]" in turn and "Current stage: mitigation" in turn and "[MITIGATION STAGE]" in turn
    assert "[RESULT]" not in turn and turn.rstrip().endswith("Reply with the JSON object for step 2.")
    assert "[STAGE CHANGE]" not in mitigation_end[-1]["content"]
    assert "Step 2 - command: kubectl patch deploy/frontend" in mitigation_end[-1]["content"]
    results = read_results(harness["logs"])
    assert results["baseline"]["stages"]["mitigation"]["commands_used"] == 1
    assert results["baseline"]["stages"]["mitigation"]["termination_reason"] == "submitted"


def test_session_mode_reasks_with_a_notice_after_an_unusable_reply(monkeypatch, harness):
    bad = StepResult(parsed=None, raw_text="not json at all", error="reply is not a JSON object", reasoning="hmm")
    backend = FakeSessionBackend([bad, submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="3", context="session") == 0
    second = backend.sessions[1]
    assert second[2] == {"role": "assistant", "content": "not json at all", "reasoning_content": "hmm"}
    assert "[NOTICE]\nYour previous reply could not be used: reply is not a JSON object." in second[3]["content"]
    assert second[3]["content"].rstrip().endswith("Reply with the JSON object for step 1.")


def test_session_mode_resends_after_a_transport_failure(monkeypatch, harness):
    failed = StepResult(parsed=None, raw_text="", error="model call failed: Timeout")
    backend = FakeSessionBackend([failed, submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="3", context="session") == 0
    assert backend.sessions[1] == backend.sessions[0]


def test_session_mode_can_leave_reasoning_out(monkeypatch, harness):
    monkeypatch.setattr(driver, "SESSION_REASONING", False)
    backend = FakeSessionBackend([command("kubectl get pods"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="3", context="session") == 0
    assert backend.sessions[1][2] == {"role": "assistant", "content": json.dumps(command("kubectl get pods"))}


def test_session_mode_needs_a_message_backend(monkeypatch, harness):
    assert run_main(monkeypatch, FakeBackend([submit(DIAGNOSIS)]), context="session") == 1
    assert harness["submissions"] == []


def test_api_backend_gives_up_at_the_hard_deadline(tmp_path, monkeypatch):
    import time as time_module

    import litellm

    from clients.baseline import backends
    from clients.baseline.backends import ApiBackend

    env = {"AGENT_API_BASE": "https://example.invalid/v1", "AGENT_API_KEY": "k", "BASELINE_API_HARD_DEADLINE_S": "0.2"}
    backend = ApiBackend("openai/glm-5.3", None, env={**env, "BASELINE_API_STREAM": "0"})
    assert backend.hard_deadline_s == 0.2

    def hangs(**kwargs):
        time_module.sleep(2)
        return None

    monkeypatch.setattr(litellm, "completion", hangs)
    monkeypatch.setattr(backends, "API_RETRY_WAIT_S", 0)
    started = time_module.monotonic()
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s1")
    assert time_module.monotonic() - started < 1.5
    assert result.parsed is None and "hard deadline" in (result.error or "")
    assert "hard deadline" in (tmp_path / "s1" / "stderr.log").read_text()


def test_api_backend_streams_by_default_and_assembles_the_reply(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import litellm

    from clients.baseline.backends import ApiBackend

    backend = ApiBackend(
        "openai/glm-5.3", "max", env={"AGENT_API_BASE": "https://example.invalid/v1", "AGENT_API_KEY": "k"}
    )
    assert backend.stream is True
    seen = {}

    def chunk(content=None, reasoning=None, finish=None, usage=None):
        delta = SimpleNamespace(content=content, reasoning_content=reasoning)
        choices = [SimpleNamespace(delta=delta, finish_reason=finish)] if content or reasoning or finish else []
        return SimpleNamespace(choices=choices, usage=usage)

    def fake_stream(**kwargs):
        seen.update(kwargs)
        usage = SimpleNamespace(
            prompt_tokens=900,
            completion_tokens=120,
            prompt_tokens_details=SimpleNamespace(cached_tokens=704),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=80),
        )
        yield chunk(reasoning="think ")
        yield chunk(reasoning="more")
        yield chunk(content='{"action": "submit", "note": "n", ')
        yield chunk(content='"command": null, "diagnosis": "d"}', finish="stop")
        yield chunk(usage=usage)

    monkeypatch.setattr(litellm, "completion", fake_stream)
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s1")
    assert seen["stream"] is True and seen["stream_options"] == {"include_usage": True}
    assert result.error is None and result.parsed["diagnosis"] == "d"
    assert result.reasoning == "think more" and (tmp_path / "s1" / "reasoning.txt").read_text() == "think more"
    assert result.usage["input_tokens"] == 900 and result.usage["cached_input_tokens"] == 704
    assert result.usage["reasoning_output_tokens"] == 80


def test_api_backend_stops_a_stream_at_the_hard_deadline(tmp_path, monkeypatch):
    import time as time_module
    from types import SimpleNamespace

    import litellm

    from clients.baseline import backends
    from clients.baseline.backends import ApiBackend

    env = {"AGENT_API_BASE": "https://example.invalid/v1", "AGENT_API_KEY": "k", "BASELINE_API_HARD_DEADLINE_S": "0.1"}
    backend = ApiBackend("openai/glm-5.3", None, env=env)
    closed = []

    class SlowStream:
        def __iter__(self):
            while True:
                time_module.sleep(0.06)
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="."), finish_reason=None)
                    ],
                    usage=None,
                )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(litellm, "completion", lambda **kwargs: SlowStream())
    monkeypatch.setattr(backends, "API_RETRY_WAIT_S", 0)
    started = time_module.monotonic()
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s1")
    assert time_module.monotonic() - started < 2
    assert result.parsed is None and "hard deadline" in (result.error or "") and closed == [True, True]


def test_api_backend_waits_out_rate_limits_then_gives_up(tmp_path, monkeypatch):
    import litellm

    from clients.baseline import backends
    from clients.baseline.backends import ApiBackend

    backend = ApiBackend(
        "openai/glm-5.3",
        None,
        env={"AGENT_API_BASE": "https://example.invalid/v1", "AGENT_API_KEY": "k", "BASELINE_API_STREAM": "0"},
    )
    monkeypatch.setattr(backends, "API_RATE_LIMIT_WAIT_S", 0)
    monkeypatch.setattr(backends, "API_RETRY_WAIT_S", 0)
    monkeypatch.setattr(backends, "API_RATE_LIMIT_RETRIES", 3)
    calls = []

    class RateLimitError(Exception):
        pass

    def limited(**kwargs):
        calls.append(1)
        raise RateLimitError("429 Too Many Requests: quota exhausted for this window")

    monkeypatch.setattr(litellm, "completion", limited)
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s1")
    # 3 waited-out rate-limit attempts, then the usual two plain attempts
    assert len(calls) == 5 and "RateLimitError" in (result.error or "")

    calls.clear()
    from types import SimpleNamespace

    def limited_then_ok(**kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise RateLimitError("429")
        message = SimpleNamespace(
            content='{"action": "submit", "note": "n", "command": null, "diagnosis": "d"}', reasoning_content=None
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr(litellm, "completion", limited_then_ok)
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s2")
    assert result.error is None and result.parsed["diagnosis"] == "d" and len(calls) == 3


def test_parse_answer_repairs_stray_backslashes_only():
    from clients.baseline.backends import _parse_answer, repair_json_text

    raw = '{"action": "command", "note": "n", "command": "grep -E \\d+ | sed \\"s/x/y/\\"", "diagnosis": null}'
    parsed, error, repaired = _parse_answer(raw)
    assert error is None and repaired is True
    assert parsed["command"] == 'grep -E \\d+ | sed "s/x/y/"'
    assert _parse_answer('{"a": "x\\\\y\\n"}') == ({"a": "x\\y\n"}, None, False)
    assert repair_json_text('"\\d \\\\ \\n \\""') == '"\\\\d \\\\ \\n \\""'
    parsed, error, repaired = _parse_answer('{"a": "unterminated}')
    assert parsed is None and "not valid JSON" in error and repaired is False


def test_parse_answer_repairs_unescaped_quotes_inside_protocol_fields():
    from clients.baseline.backends import _parse_answer

    raw = (
        '{"action": "command", "note": "Inspect mongo", "command": "kubectl exec deploy/mongo -- mongo --eval '
        '\'db = db.getSiblingDB("social-graph"); db.stats()\'", "diagnosis": null}'
    ).replace('\\"', '"')
    assert '"social-graph"' in raw  # the quotes really are unescaped
    parsed, error, repaired = _parse_answer(raw)
    assert error is None and repaired is True
    assert parsed["command"].endswith('getSiblingDB("social-graph"); db.stats()\'') and parsed["diagnosis"] is None
    both = '{"action": "command", "note": "n", "command": "grep -E \\d+ "a b" file", "diagnosis": null}'.replace(
        '\\"', '"'
    )
    parsed, error, repaired = _parse_answer(both)
    assert error is None and repaired is True and parsed["command"] == 'grep -E \\d+ "a b" file'
    # a value that is the last field, followed by the closing brace
    last = '{"action": "submit", "note": "n", "command": null, "diagnosis": "the "frontend" pod fails"}'
    parsed, error, repaired = _parse_answer(last)
    assert error is None and parsed["diagnosis"] == 'the "frontend" pod fails'


def test_api_backend_reports_repaired_replies(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import litellm

    from clients.baseline.backends import ApiBackend

    backend = ApiBackend(
        "openai/glm-5.3",
        None,
        env={"AGENT_API_BASE": "https://example.invalid/v1", "AGENT_API_KEY": "k", "BASELINE_API_STREAM": "0"},
    )

    def stray(**kwargs):
        message = SimpleNamespace(
            content='{"action": "command", "note": "n", "command": "grep \\d", "diagnosis": null}',
            reasoning_content=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)

    monkeypatch.setattr(litellm, "completion", stray)
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s1")
    assert result.error is None and result.repaired is True and result.parsed["command"] == "grep \\d"


def test_parse_answer_reads_deepseek_dsml_markup():
    from clients.baseline.backends import _parse_answer, parse_dsml

    bar = "\uff5c\uff5c"
    raw = (
        f'<{bar}DSML{bar} calls>\n<{bar}DSML{bar} invoke name="command">\n'
        f'<{bar}DSML{bar} parameter name="command" string="true">kubectl get pods -n x && echo \'---\'</{bar}DSML{bar} parameter>\n'
        f'<{bar}DSML{bar} parameter name="note" string="true">Listing pods</{bar}DSML{bar} parameter>\n'
        f'<{bar}DSML{bar} parameter name="diagnosis" string="false">null</{bar}DSML{bar} parameter>\n'
        f"</{bar}DSML{bar} invoke>\n</{bar}DSML{bar} calls>"
    )
    parsed, error, repaired = _parse_answer(raw)
    assert error is None and repaired is True
    assert parsed == {
        "action": "command",
        "command": "kubectl get pods -n x && echo '---'",
        "note": "Listing pods",
        "diagnosis": None,
    }
    assert parse_dsml("plain text") is None and parse_dsml("DSML mentioned but no markup") is None


def test_parse_budget():
    assert driver.parse_budget("0") == 0
    assert driver.parse_budget("3") == 3
    assert driver.parse_budget("unlimited") is None
    with pytest.raises(ValueError):
        driver.parse_budget("-1")


def test_is_external_submit():
    assert driver.is_external_submit("curl -X POST http://host.docker.internal:8000/submit -d '{}'")
    assert driver.is_external_submit("python3 - <<EOF\nrequests.post('http://x:8000/submit_mcp/sse')\nEOF")
    assert not driver.is_external_submit("kubectl get pods -n hotel-reservation")
    assert not driver.is_external_submit("kubectl logs deploy/submitter")


def test_run_command_captures_output_and_exit_code(tmp_path):
    result = driver.run_command("echo out; echo err 1>&2; exit 3", timeout=10, cwd=str(tmp_path))
    assert result.stdout == "out\n" and result.stderr == "err\n" and result.exit_code == 3
    assert result.timed_out is False
    slow = driver.run_command("sleep 5", timeout=1, cwd=str(tmp_path))
    assert slow.timed_out is True and slow.exit_code == 124


def test_codex_usage_mapping_and_command_flags(tmp_path, monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1200,
                "cached_input_tokens": 900,
                "cache_write_input_tokens": 100,
                "output_tokens": 80,
                "reasoning_output_tokens": 30,
            },
        },
    ]
    usage = parse_codex_usage(events)
    assert usage["input_tokens"] == 1200
    assert usage["cached_input_tokens"] == 900
    assert usage["cache_creation_input_tokens"] == 100
    assert usage["reasoning_output_tokens"] == 30
    assert usage["total_tokens"] == 1280
    assert parse_codex_usage([])["input_tokens"] is None

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    env = {"CODEX_HOME": str(home), "OPENAI_API_KEY": "sk-test", "PATH": "/usr/bin"}
    backend = CodexExecBackend("openai/gpt-5.5", "medium", env=env)
    assert backend.model == "gpt-5.5"
    assert "OPENAI_API_KEY" not in backend.environment()
    cmd = backend.command(tmp_path / "schema.json", tmp_path / "answer.json")
    assert cmd[:4] == ["codex", "-a", "never", "exec"]
    assert "--output-schema" in cmd and "--ephemeral" in cmd and cmd[-1] == "-"
    assert "-c" in cmd and 'model_reasoning_effort="medium"' in cmd
    assert (
        "features={"
        + ",".join(
            f"{k}=false"
            for k in (
                "shell_tool",
                "unified_exec",
                "apps",
                "multi_agent",
                "plugins",
                "view_image",
                "image_generation",
                "browser_use",
                "computer_use",
                "skill_search",
                "workspace_dependencies",
            )
        )
        + "}"
        in cmd
    )
    no_auth = CodexExecBackend("gpt-5.5", None, env={"CODEX_HOME": str(tmp_path / "missing"), "OPENAI_API_KEY": "sk"})
    assert no_auth.environment()["OPENAI_API_KEY"] == "sk"


def test_preflight_exits_zero_on_ok_reply(monkeypatch):
    backend = FakeBackend([{"ok": True}])
    monkeypatch.setattr(driver, "make_backend", lambda name, model, effort: backend)
    with pytest.raises(SystemExit) as exc:
        driver.run_preflight()
    assert exc.value.code == 0
    assert backend.calls[0][1]["required"] == ["ok"]


def test_protocol_task_text_matches_prompt_used_by_driver(monkeypatch, harness):
    backend = FakeBackend([submit(DIAGNOSIS)])
    run_main(monkeypatch, backend, max_commands="0")
    prompt = backend.calls[0][0]
    assert protocol.build_task_text(APP) in prompt


def test_default_backend_name_prefers_explicit_then_api_endpoint(monkeypatch):
    monkeypatch.setattr(driver, "BACKEND_NAME", "")
    monkeypatch.delenv("AGENT_API_BASE", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    assert driver.default_backend_name() == "codex"
    monkeypatch.setenv("AGENT_API_BASE", "https://api.z.ai/api/paas/v4")
    assert driver.default_backend_name() == "api"
    monkeypatch.setattr(driver, "BACKEND_NAME", "codex")
    assert driver.default_backend_name() == "codex"


def test_api_backend_request_and_response_mapping(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clients.baseline.backends import ApiBackend

    env = {
        "AGENT_API_BASE": "https://api.z.ai/api/paas/v4",
        "AGENT_API_KEY": "secret-key-123",
        "BASELINE_EXTRA_BODY": '{"thinking": {"type": "enabled"}}',
        "BASELINE_MAX_TOKENS": "4096",
    }
    backend = ApiBackend("openai/glm-5.3", "max", env={**env, "BASELINE_API_STREAM": "0"})
    request = backend.request("prompt", {"type": "object"})
    assert request["model"] == "openai/glm-5.3" and request["api_base"] == env["AGENT_API_BASE"]
    assert request["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    assert request["max_tokens"] == 4096 and request["temperature"] == 0
    assert "secret-key-123" not in json.dumps(request["messages"])

    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        usage = SimpleNamespace(
            prompt_tokens=1500,
            completion_tokens=400,
            prompt_tokens_details=SimpleNamespace(cached_tokens=1000),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=350),
        )
        message = SimpleNamespace(
            content='```json\n{"action": "submit", "note": "n", "command": null, "diagnosis": "d"}\n```',
            reasoning_content="thinking...",
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage)

    import litellm

    monkeypatch.setattr(litellm, "completion", fake_completion)
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s1")
    assert result.error is None and result.parsed["diagnosis"] == "d"
    assert result.usage["input_tokens"] == 1500 and result.usage["cached_input_tokens"] == 1000
    assert result.usage["reasoning_output_tokens"] == 350 and result.usage["total_tokens"] == 1900
    assert (tmp_path / "s1" / "reasoning.txt").read_text() == "thinking..."
    assert calls[0]["extra_body"]["reasoning_effort"] == "max"

    messages = [
        backend.system_message({"type": "object"}),
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "{}", "reasoning_content": "earlier thinking"},
        {"role": "user", "content": "[RESULT] ..."},
    ]
    result = backend.complete_messages(messages, step_dir=tmp_path / "s2")
    assert calls[-1]["messages"] == messages and calls[-1]["extra_body"] == request["extra_body"]
    assert result.reasoning == "thinking..." and result.parsed["diagnosis"] == "d"
    assert json.loads((tmp_path / "s2" / "messages.json").read_text()) == messages
    assert (tmp_path / "s2" / "prompt.txt").read_text() == "[RESULT] ..."

    def truncated(**kwargs):
        message = SimpleNamespace(content='{"action": "submit", "note": "n"', reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="length")], usage=None)

    monkeypatch.setattr(litellm, "completion", truncated)
    result = backend.complete_json("prompt", {"type": "object"}, step_dir=tmp_path / "s2")
    assert result.parsed is None and "max_tokens=4096" in result.error
