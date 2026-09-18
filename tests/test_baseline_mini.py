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
        if "/submit" in cmd and state.get("conductor_accepts_curl"):  # the conductor advances the stage
            state["current"] = "mitigation" if state["current"] == "diagnosis" else "done"
            return tools.CommandResult('{"status": "200", "message": "Submission received"}\n', "", 0, 0.05)
        if cmd.startswith("sleep"):
            return tools.CommandResult("", "", 124, 60.0, timed_out=True)
        return tools.CommandResult("pod-a Running\n", "warn\n", 0, 0.01)

    monkeypatch.setattr(driver, "run_command", run_command)
    monkeypatch.setattr(sys, "argv", ["driver"])
    return state


def run_main(monkeypatch, backend, *, max_calls="unlimited", mode="full", hard_cap=80, submit="marker"):
    monkeypatch.setattr(driver, "SUBMIT_RAW", submit)
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


def test_the_workflow_is_part_of_the_prompt_and_stays_domain_general():
    text = mini.instance_text(APP, mode="full", max_commands=None)
    assert "## How to work" in text and text.index("## How to work") > text.index("## How to submit")
    curl = mini.instance_text(APP, mode="full", max_commands=None, submit_mode="curl")
    assert "## How to work" in curl and "new subshell" in curl
    assert "one thing at a time" in mini.mitigation_text(None)
    # it may say how to investigate, never where to look or what the judge scores
    for banned in ("kubectl", "Kubernetes", "pod", "CronJob", "Service", "namespace", "root cause", "affected"):
        assert banned.lower() not in mini.WORKFLOW_TEXT.lower(), banned


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


def test_budget_notice_counts_from_the_first_command_and_turns_urgent_at_the_end():
    early = mini.budget_notice(used=3, cap=80, seconds_left=1400, submit_mode="marker")
    assert early == "\n<budget>command 3 of 80, about 23 minutes left in this stage</budget>"
    near_cap = mini.budget_notice(used=70, cap=80, seconds_left=1400, submit_mode="marker")
    assert "10 commands left in this stage" in near_cap and "submit your best answer now" in near_cap
    assert f"must start with `{MARKER}`" in near_cap
    near_time = mini.budget_notice(used=3, cap=80, seconds_left=120, submit_mode="curl")
    assert "about 2 minutes left" in near_time and "as the task instruction describes" in near_time
    both = mini.budget_notice(used=79, cap=80, seconds_left=30, submit_mode="marker")
    assert "1 command and about 0 minutes left" in both


def test_every_observation_of_an_unlimited_arm_carries_the_budget(monkeypatch, harness):
    backend = FakeBackend([block("a"), block("b"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, hard_cap=80) == 0
    assert "<budget>command 1 of 80" in backend.calls[1][-1]["content"]
    assert "<budget>command 2 of 80" in backend.calls[2][-1]["content"]


def test_a_budget_arm_is_left_alone(monkeypatch, harness):
    backend = FakeBackend([block("a"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="3") == 0
    assert "<budget>" not in backend.calls[1][-1]["content"]


def test_hard_cap_asks_the_unlimited_arm_to_submit_what_it_has(monkeypatch, harness):
    backend = FakeBackend([block("a"), block("b"), submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, hard_cap=2) == 0
    assert harness["commands"][:2] == ["a", "b"]
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]
    notice = backend.calls[2][-1]["content"]
    assert notice.startswith("You have reached the limit of 2 commands for this stage.")
    assert f"must start with `{MARKER}`" in notice
    wrap_up = [r for r in read_transcript(harness["logs"]) if r["type"] == "wrap_up"]
    assert [(r["reason"], r["commands"]) for r in wrap_up] == [("hard_cap", 2)]
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "submitted"


def test_stage_ends_without_a_submission_when_the_wrap_up_is_ignored(monkeypatch, harness):
    backend = FakeBackend([block(c) for c in "abcdef"])
    monkeypatch.setattr(driver, "WRAP_UP_CALLS", 2)
    assert run_main(monkeypatch, backend, hard_cap=2) == driver.EXIT_NO_SUBMISSION
    assert harness["commands"] == ["a", "b", "c", "d"] and harness["submissions"] == []
    assert len(backend.calls) == 4  # two before the cap, two after the notice
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "hard_cap_no_submission"


def test_the_curl_arm_is_told_to_submit_the_way_the_task_describes(monkeypatch, harness):
    harness["conductor_accepts_curl"] = True
    backend = FakeBackend([block("a"), block("curl -X POST http://host.docker.internal:8000/submit -d '{}'")])
    assert run_main(monkeypatch, backend, hard_cap=1, submit="curl") == 0
    notice = backend.calls[1][-1]["content"]
    assert "exactly as the task instruction describes" in notice and MARKER not in notice
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "submitted_by_command"


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


# --- curl mode: the CLI agents' condition ---------------------------------------------------


def test_resolve_submit_mode():
    assert mini.resolve_submit_mode("auto", mode="full", max_commands=None) == "curl"
    assert mini.resolve_submit_mode("auto", mode="full", max_commands=3) == "marker"
    assert mini.resolve_submit_mode("auto", mode="no_mechanism", max_commands=None) == "marker"
    assert mini.resolve_submit_mode("marker", mode="full", max_commands=None) == "marker"
    with pytest.raises(ValueError):
        mini.resolve_submit_mode("curl", mode="full", max_commands=3)
    with pytest.raises(ValueError):
        mini.resolve_submit_mode("nope", mode="full", max_commands=None)


def test_curl_mode_instance_is_the_task_plus_the_note_and_the_workflow():
    text = mini.instance_text(APP, mode="full", max_commands=None, submit_mode="curl")
    assert text == mini.build_task_text(APP).rstrip() + mini.CURL_INSTANCE_NOTE + mini.WORKFLOW_TEXT
    assert "Important Rules" not in text and MARKER not in text


def test_curl_mode_lets_the_model_submit_itself_through_both_stages(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    harness["conductor_accepts_curl"] = True
    curl_diag = block(
        "curl -s -X POST http://localhost:8000/submit -H 'Content-Type: application/json' -d '{\"solution\": \"the selector is wrong\"}'"
    )
    curl_fix = block(
        "curl -s -X POST http://localhost:8000/submit -H 'Content-Type: application/json' -d '{\"solution\": \"\"}'"
    )
    backend = FakeBackend([block("kubectl get pods"), curl_diag, block("kubectl patch svc frontend -p '{}'"), curl_fix])
    assert run_main(monkeypatch, backend, submit="auto") == 0
    assert harness["submissions"] == []  # the driver posts nothing; the model did it
    assert len(harness["commands"]) == 4
    first = backend.calls[0]
    assert first[1]["content"] == mini.build_task_text(APP).rstrip() + mini.CURL_INSTANCE_NOTE + mini.WORKFLOW_TEXT
    # no stage-change message: the conversation just continues with the observation of the curl command
    third = backend.calls[2]
    assert third[-1]["role"] == "user" and third[-1]["content"].startswith("<returncode>0</returncode>")
    assert "Submission received" in third[-1]["content"] and "Current stage: mitigation" not in third[-1]["content"]
    results = read_results(harness["logs"])
    assert results["baseline"]["submit_mode"] == "curl" and results["baseline"]["submitted_stages"] == [
        "diagnosis",
        "mitigation",
    ]
    assert results["baseline"]["stages"]["diagnosis"]["termination_reason"] == "submitted_by_command"
    assert results["baseline"]["stages"]["mitigation"]["termination_reason"] == "submitted_by_command"
    assert results["baseline"]["stages"]["diagnosis"]["commands_used"] == 2  # the curl counts as a command it ran
    submits = [r for r in read_transcript(harness["logs"]) if r["type"] == "submit"]
    assert [r["by"] for r in submits] == ["command", "command"]


def test_curl_mode_ignores_the_marker_and_never_refuses_curl(monkeypatch, harness):
    harness["conductor_accepts_curl"] = True
    backend = FakeBackend([block(f"echo {MARKER}"), block("curl -X POST http://localhost:8000/submit -d '{}'")])
    assert run_main(monkeypatch, backend, submit="auto") == 0
    assert harness["submissions"] == []
    second = backend.calls[1]
    assert second[-1]["content"].startswith("<returncode>0</returncode>\n<output>\n" + MARKER)
    assert (
        read_results(harness["logs"])["baseline"]["stages"]["diagnosis"]["termination_reason"] == "submitted_by_command"
    )


def test_curl_mode_with_a_budget_is_refused_at_startup(monkeypatch, harness):
    backend = FakeBackend([submit_block(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="3", submit="curl") == driver.EXIT_INFRA
    assert harness["submissions"] == [] and backend.calls == []
