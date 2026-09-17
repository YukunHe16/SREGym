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
    state = {"stages": ["diagnosis", "done"], "submissions": [], "commands": [], "logs": tmp_path}
    monkeypatch.setattr(driver, "AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(driver, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(driver, "resolve_problem_id", lambda cli_problem_id=None: "anon_test")
    monkeypatch.setattr(driver, "get_app_info", lambda: APP)
    monkeypatch.setattr(driver, "current_stage", lambda: "diagnosis")
    monkeypatch.setattr(driver, "wait_for_stage", lambda stages, timeout: state["stages"].pop(0))

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


def run_main(monkeypatch, backend, *, max_commands="10", mode="full", hard_cap=60):
    monkeypatch.setattr(driver, "MAX_COMMANDS_RAW", max_commands)
    monkeypatch.setattr(driver, "SUBMISSION_MODE", mode)
    monkeypatch.setattr(driver, "HARD_CAP", hard_cap)
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
    kinds = [record["type"] for record in read_transcript(harness["logs"])]
    assert kinds[:2] == ["meta", "model_call"] and "submit" in kinds and kinds[-1] == "end"


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


def test_mitigation_stage_gets_the_empty_submission(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend([submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="0") == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis"), ("", "mitigation")]
    assert read_results(harness["logs"])["baseline"]["submitted_stages"] == ["diagnosis", "mitigation"]


def test_attempt_starting_at_mitigation_skips_the_model(monkeypatch, harness):
    harness["stages"] = ["mitigation", "done"]
    backend = FakeBackend([])
    assert run_main(monkeypatch, backend, max_commands="3") == 0
    assert backend.calls == []
    assert harness["submissions"] == [("", "mitigation")]


def test_external_submission_is_detected_and_not_resubmitted(monkeypatch, harness):
    monkeypatch.setattr(driver, "current_stage", lambda: "tearing_down")
    backend = FakeBackend([command("python3 -c 'import requests'"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_commands="3") == 3
    assert harness["submissions"] == []
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "external_submission"


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
