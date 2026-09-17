import json
import sys
from pathlib import Path

import pytest

from clients.baseline import driver, mini, tools
from clients.baseline.backends import Reply
from clients.harness.token_usage import usage_metrics
from sregym.run_artifacts import _find_token_hit

APP = {"app_name": "Hotel Reservation", "namespace": "hotel-reservation", "descriptions": "A hotel app."}
REAL_PROBLEM_ID = "service_wrong_pod_selection_hotel_reservation"
DIAGNOSIS = "The frontend Service selector also matches the search pod."
MARKER = mini.MARKER


class FakeBackend:
    name = "api"
    model = "fake-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def version(self):
        return "fake-1"

    @staticmethod
    def system_message(text):
        return {"role": "system", "content": text}

    def complete(self, messages, tool_specs, *, step_dir):
        assert tool_specs == []
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(json.dumps(messages))
        self.calls.append(json.loads(json.dumps(messages)))
        reply = self.replies.pop(0)
        if isinstance(reply, str):
            reply = Reply(content=reply, reasoning="thinking")
        if reply.usage.get("input_tokens") is None:
            reply.usage = usage_metrics(
                input_tokens=100, output_tokens=10, cached_input_tokens=50, reasoning_output_tokens=5
            )
        return reply


def block(command, thought="THOUGHT: look"):
    return f"{thought}\n\n```bash\n{command}\n```\n"


def submit_block(text):
    return block(f"cat <<'EOF'\n{MARKER}\n{text}\nEOF")


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = {"stages": ["diagnosis", "done"], "submissions": [], "commands": [], "logs": tmp_path, "current": None}
    monkeypatch.setattr(driver, "AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(driver, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(driver, "PROTOCOL", "mini")
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
        if MARKER in cmd:  # emulate the heredoc / echo: print what the command would print
            body = cmd.split("\n", 1)[1].rsplit("\nEOF", 1)[0] if "<<'EOF'" in cmd else MARKER
            return tools.CommandResult(body + "\n", "", 0, 0.01)
        if cmd.startswith("sleep"):
            return tools.CommandResult("", "", 124, 60.0, timed_out=True)
        return tools.CommandResult("pod-a Running\n", "warn\n", 0, 0.01)

    monkeypatch.setattr(driver, "run_command", run_command)
    monkeypatch.setattr(sys, "argv", ["driver"])
    return state


def run_main(monkeypatch, backend, *, max_calls="unlimited", mode="full", hard_cap=80):
    monkeypatch.setattr(driver, "MAX_COMMANDS_RAW", max_calls)
    monkeypatch.setattr(driver, "SUBMISSION_MODE", mode)
    monkeypatch.setattr(driver, "HARD_CAP", hard_cap)
    monkeypatch.setattr(driver, "make_backend", lambda model, effort: backend)
    with pytest.raises(SystemExit) as exc:
        driver.main()
    return exc.value.code


def read_results(logs: Path) -> dict:
    files = list(logs.glob("baseline_results_*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def read_transcript(logs: Path) -> list[dict]:
    return [json.loads(line) for line in (logs / "baseline_transcript.jsonl").read_text().splitlines()]


# --- templates and parsing ------------------------------------------------------------


def test_instance_text_wraps_the_verbatim_task_with_mini_rules():
    text = mini.instance_text(APP, mode="full", max_commands=None)
    assert text.startswith(mini.build_task_text(APP).rstrip())
    assert "Every response must contain exactly one action" in text
    assert "executed in a new subshell" in text
    assert f"starts with the line `{MARKER}`" in text and "cat <<'EOF'" in text
    assert "You may run at most" not in text
    assert "You may run at most 3 commands in this stage" in mini.instance_text(APP, mode="full", max_commands=3)
    assert "cannot run any commands" in mini.instance_text(APP, mode="full", max_commands=0)
    assert "faulty_component:" in mini.instance_text(APP, mode="no_mechanism", max_commands=None)
    assert "exactly ONE bash code block" in mini.system_text()


def test_parse_action_requires_exactly_one_block():
    assert mini.parse_action(block("kubectl get pods")) == ("kubectl get pods", 1)
    assert mini.parse_action("no block here") == (None, 0)
    assert mini.parse_action(block("a") + block("b")) == (None, 2)
    assert "found 2 actions" in mini.format_error_text(2)


def test_observation_and_timeout_texts():
    short = mini.observation_text(0, "hello")
    assert short == "<returncode>0</returncode>\n<output>\nhello\n</output>"
    long = mini.observation_text(1, "x" * 12000)
    assert (
        "<warning>" in long and "<output_head>" in long and "2000 characters elided" in long and "<output_tail>" in long
    )
    assert "timed out and has been killed" in mini.timeout_text("sleep 99", "")


def test_finished_and_three_fields():
    assert mini.finished(f"{MARKER}\nline one\nline two\n", 0) == "line one\nline two"
    assert mini.finished(f"{MARKER}\n", 0) == ""
    assert mini.finished(f"{MARKER}\ntext", 1) is None
    assert mini.finished("other\n" + MARKER, 0) is None
    fields, error = mini.parse_three_fields("faulty_component: deployment/x\naffected_components: a, b\nsymptom: 500s")
    assert error is None and fields["symptom"] == "500s"
    assert (
        mini.compose_three_fields(fields)
        == "Faulty component: deployment/x. Affected components: a, b. Observed symptom: 500s."
    )
    assert mini.compose_three_fields({**fields, "affected_components": "none"}).count("none identified") == 1
    assert "missing: symptom" in mini.parse_three_fields("faulty_component: x\naffected_components: y")[1]
    assert mini.mechanism_guard_hits(
        {"faulty_component": "x", "affected_components": "", "symptom": "fails because it is wrong"}
    ) == ["because", "wrong"]


# --- the loop ---------------------------------------------------------------------------


def test_commands_then_marker_submission(monkeypatch, harness):
    backend = FakeBackend(
        [block("kubectl get pods -n hotel-reservation"), block("kubectl get svc"), submit_block(DIAGNOSIS)]
    )
    assert run_main(monkeypatch, backend) == 0
    assert harness["commands"][:2] == ["kubectl get pods -n hotel-reservation", "kubectl get svc"]
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]
    first = backend.calls[0]
    assert first[0]["content"] == mini.system_text() and first[1]["content"].startswith(
        mini.build_task_text(APP).rstrip()
    )
    second = backend.calls[1]
    assert second[-1]["role"] == "user" and second[-1]["content"].startswith(
        "<returncode>0</returncode>\n<output>\npod-a Running\nwarn"
    )
    assert second[-2]["reasoning_content"] == "thinking"
    results = read_results(harness["logs"])
    assert results["baseline"]["protocol"] == "mini" and results["baseline"]["commands_used"] == 2
    assert results["baseline"]["termination_reason"] == "submitted" and results["success"] is True
    records = read_transcript(harness["logs"])
    assert [r["command"] for r in records if r["type"] == "command"][-1].startswith("cat <<'EOF'")
    assert [r for r in records if r["type"] == "command"][-1]["submission_attempt"] is True
    assert [r["action"] for r in records if r["type"] == "model_call"][0] == "kubectl get pods -n hotel-reservation"


def test_format_errors_get_the_template_and_three_in_a_row_end_without_submission(monkeypatch, harness):
    backend = FakeBackend(["no block", block("a") + block("b"), "still none"])
    assert run_main(monkeypatch, backend) == driver.EXIT_NO_SUBMISSION
    assert harness["submissions"] == [] and harness["commands"] == []
    assert backend.calls[1][-1]["content"].startswith(
        "Please always provide EXACTLY ONE action in triple backticks, found 0 actions."
    )
    assert "found 2 actions" in backend.calls[2][-1]["content"]
    results = read_results(harness["logs"])
    assert results["baseline"]["termination_reason"] == "repeated_format_error" and results["success"] is False


def test_format_error_counter_resets_after_a_good_action(monkeypatch, harness):
    backend = FakeBackend(["none", block("kubectl get pods"), "none", "none", submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]


def test_unlimited_arm_stops_at_the_hard_cap_without_submitting(monkeypatch, harness):
    backend = FakeBackend([block("a"), block("b"), block("c"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, hard_cap=2) == driver.EXIT_NO_SUBMISSION
    assert harness["commands"] == ["a", "b"] and harness["submissions"] == []
    assert len(backend.calls) == 2
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "hard_cap_no_submission"


def test_budget_arm_gets_one_notice_then_may_only_submit(monkeypatch, harness):
    backend = FakeBackend([block("a"), block("b"), block("c"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="2") == 0
    assert harness["commands"] == ["a", "b", f"cat <<'EOF'\n{MARKER}\n{DIAGNOSIS}\nEOF"]
    third = backend.calls[2]
    assert third[-1]["content"].startswith("You have used the 2 commands allowed in this stage.")
    fourth = backend.calls[3]
    assert fourth[-1]["content"].startswith("<returncode>126</returncode>\n<output>\nrejected: the command budget")
    results = read_results(harness["logs"])
    assert results["baseline"]["commands_used"] == 2 and results["baseline"]["termination_reason"] == "submitted"
    refused = [r for r in read_transcript(harness["logs"]) if r["type"] == "command" and r.get("refused")]
    assert [r["refused"] for r in refused] == ["budget_exhausted"]


def test_zero_budget_arm_only_submits(monkeypatch, harness):
    backend = FakeBackend([submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="0") == 0
    first = backend.calls[0]
    assert "cannot run any commands" in first[1]["content"] and first[-1]["content"].startswith(
        "5. You cannot run any commands"
    )
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]
    assert read_results(harness["logs"])["baseline"]["commands_used"] == 0


def test_no_mechanism_three_lines_guard_and_compose(monkeypatch, harness):
    leaky = submit_block(
        "faulty_component: service/frontend\naffected_components: search\nsymptom: fails because the selector is wrong"
    )
    clean = submit_block(
        "faulty_component: service/frontend\naffected_components: search\nsymptom: requests fail intermittently"
    )
    backend = FakeBackend([submit_block("faulty_component: service/frontend"), leaky, clean])
    assert run_main(monkeypatch, backend, mode="no_mechanism") == 0
    assert "missing: affected_components, symptom" in backend.calls[1][-1]["content"]
    assert "explained a mechanism" in backend.calls[2][-1]["content"]
    assert harness["submissions"] == [
        (
            "Faulty component: service/frontend. Affected components: search. Observed symptom: requests fail intermittently.",
            "diagnosis",
        )
    ]
    assert read_results(harness["logs"])["baseline"]["mechanism_guard_tripped"] is False


def test_empty_diagnosis_after_marker_is_rejected(monkeypatch, harness):
    backend = FakeBackend([block(f"echo {MARKER}"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    assert "diagnosis after the marker line is empty" in backend.calls[1][-1]["content"]
    assert read_results(harness["logs"])["baseline"]["commands_used"] == 0


def test_external_submit_command_is_refused_and_counted(monkeypatch, harness):
    backend = FakeBackend(
        [block("curl -X POST http://host.docker.internal:8000/submit -d '{}'"), submit_block(DIAGNOSIS)]
    )
    assert run_main(monkeypatch, backend, max_calls="3") == 0
    assert harness["commands"] == [f"cat <<'EOF'\n{MARKER}\n{DIAGNOSIS}\nEOF"]
    assert backend.calls[1][-1]["content"].startswith(
        "<returncode>126</returncode>\n<output>\nrejected: the driver does not run"
    )
    assert read_results(harness["logs"])["baseline"]["commands_used"] == 1


def test_timeout_uses_the_timeout_template(monkeypatch, harness):
    backend = FakeBackend([block("sleep 99"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    assert backend.calls[1][-1]["content"].startswith(
        "The last command <command>sleep 99</command> timed out and has been killed."
    )
    assert read_results(harness["logs"])["baseline"]["commands_used"] == 1


def test_two_transport_failures_end_without_submission(monkeypatch, harness):
    backend = FakeBackend([Reply(error="model call failed: boom"), Reply(error="model call failed: boom")])
    assert run_main(monkeypatch, backend) == driver.EXIT_MODEL_FAILURE
    assert harness["submissions"] == []
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "model_failure"


def test_mitigation_continues_the_conversation_and_ends_with_the_marker(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend(
        [
            submit_block(DIAGNOSIS),
            block("kubectl patch svc frontend -n hotel-reservation -p '{}'"),
            block(f"echo {MARKER}"),
        ]
    )
    assert run_main(monkeypatch, backend) == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis"), ("", "mitigation")]
    mitigation_call = backend.calls[1]
    assert mitigation_call[: len(backend.calls[0])] == backend.calls[0]
    assert mitigation_call[-1]["content"].startswith("The diagnosis has been submitted. Current stage: mitigation.")
    results = read_results(harness["logs"])
    assert results["baseline"]["submitted_stages"] == ["diagnosis", "mitigation"]
    assert results["baseline"]["stages"]["mitigation"]["commands_used"] == 1
    assert results["baseline"]["stages"]["mitigation"]["termination_reason"] == "submitted"


def test_attempt_starting_at_mitigation(monkeypatch, harness):
    harness["stages"] = ["mitigation", "done"]
    backend = FakeBackend([block(f"echo {MARKER}")])
    assert run_main(monkeypatch, backend) == 0
    assert backend.calls[0][-1]["content"].startswith("The diagnosis has been submitted. Current stage: mitigation.")
    assert harness["submissions"] == [("", "mitigation")]


def test_external_submission_is_detected(monkeypatch, harness):
    monkeypatch.setattr(driver, "current_stage", lambda: "tearing_down")
    backend = FakeBackend([block("python3 -c 'import requests'"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="3") == 3
    assert harness["submissions"] == []


def test_no_real_problem_id_reaches_the_logs(monkeypatch, harness):
    monkeypatch.setenv("SREGYM_ARTIFACT_ID", "anon_test")
    backend = FakeBackend([block("kubectl get pods"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    assert _find_token_hit(harness["logs"], REAL_PROBLEM_ID) is None
