import copy
import json
import sys
from pathlib import Path

import pytest

from clients.baseline import driver, protocol, tools
from clients.baseline.backends import Reply, ToolCall
from clients.baseline.tools import Toolbox, ToolSpec
from clients.harness.token_usage import usage_metrics
from sregym.run_artifacts import _find_token_hit

APP = {"app_name": "Hotel Reservation", "namespace": "hotel-reservation", "descriptions": "A hotel app."}
REAL_PROBLEM_ID = "service_wrong_pod_selection_hotel_reservation"
DIAGNOSIS = "The frontend Service selector also matches the search pod."


class FakeBackend:
    name = "api"
    model = "fake-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[list[dict], list[str]]] = []

    def version(self):
        return "fake-1"

    @staticmethod
    def system_message(text):
        return {"role": "system", "content": text}

    def complete(self, messages, tool_specs, *, step_dir):
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(json.dumps(messages))
        self.calls.append((copy.deepcopy(messages), [t.name for t in tool_specs]))
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(messages, tool_specs)
        if reply.usage.get("input_tokens") is None:
            reply.usage = usage_metrics(
                input_tokens=100, output_tokens=10, cached_input_tokens=50, reasoning_output_tokens=5
            )
        return reply


_ids = iter(range(1, 10_000))


def call(name, **args):
    return ToolCall(f"call_{next(_ids)}", name, json.dumps(args))


def tool_reply(*calls, content="", reasoning="thinking"):
    return Reply(content=content, reasoning=reasoning, tool_calls=list(calls))


def bash(command):
    return tool_reply(call("bash", command=command))


def submit(text):
    return tool_reply(call("submit", diagnosis=text))


def submit_nomech(component, affected, symptom):
    return tool_reply(call("submit", faulty_component=component, affected_components=affected, symptom=symptom))


def text_reply(content):
    return Reply(content=content, reasoning=None)


class FakeMcp:
    def __init__(self, specs):
        self.specs = specs
        self.calls = []

    def call(self, spec, args):
        self.calls.append((spec.name, args))
        return f"{spec.name} result"

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = {
        "stages": ["diagnosis", "done"],
        "submissions": [],
        "commands": [],
        "logs": tmp_path,
        "current": None,
        "toolbox": None,
    }
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
        return tools.CommandResult("pod-a Running\n", "", 0, 0.01)

    monkeypatch.setattr(tools, "run_command", run_command)

    def build_toolbox(problem_id, work_dir):
        toolbox = state["toolbox"] or Toolbox(local=["bash"], command_timeout=5, work_dir=work_dir)
        return toolbox, {
            "local": toolbox.local,
            "mcp_servers": [],
            "mcp_tools": [s.name for s in toolbox.specs() if s.kind == "mcp"],
            "mcp_error": None,
        }

    monkeypatch.setattr(driver, "build_toolbox", build_toolbox)
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


def tool_messages(messages):
    return [m["content"] for m in messages if m["role"] == "tool"]


def test_zero_budget_offers_only_submit(monkeypatch, harness):
    backend = FakeBackend([submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="0") == 0
    messages, offered = backend.calls[0]
    assert offered == ["submit"]
    assert messages[0]["role"] == "system" and "which component is at fault" in messages[0]["content"]
    assert "[TASK]\n" + protocol.build_task_text(APP).rstrip() in messages[1]["content"]
    assert "cannot call any tool other than submit" in messages[1]["content"]
    assert "[NOTICE]" in messages[2]["content"] and "budget exhausted" in messages[2]["content"]
    assert harness["commands"] == [] and harness["submissions"] == [(DIAGNOSIS, "diagnosis")]
    results = read_results(harness["logs"])
    assert results["problem_id"] == "anon_test" and results["success"] is True
    assert results["baseline"]["commands_used"] == 0 and results["baseline"]["model_calls"] == 1
    assert results["baseline"]["termination_reason"] == "budget_exhausted_forced_submit"
    assert results["baseline"]["context"] == "session" and results["baseline"]["tools"]["local"] == ["bash"]
    assert results["usage_metrics"]["input_tokens"] == 100 and results["usage_metrics"]["token_metrics_version"] == 2
    kinds = [r["type"] for r in read_transcript(harness["logs"])]
    assert kinds[:2] == ["meta", "model_call"] and "submit" in kinds and kinds[-1] == "end"


def test_budget_counts_refused_commands_and_forces_submit(monkeypatch, harness):
    backend = FakeBackend(
        [
            bash("kubectl get pods -n hotel-reservation"),
            bash("curl -X POST http://host.docker.internal:8000/submit -d '{}'"),
            bash("kubectl logs deploy/frontend"),
            bash("kubectl get svc"),
            submit(DIAGNOSIS),
        ]
    )
    assert run_main(monkeypatch, backend, max_calls="3") == 0
    assert harness["commands"] == ["kubectl get pods -n hotel-reservation", "kubectl logs deploy/frontend"]
    fourth_messages, fourth_offered = backend.calls[3]
    assert fourth_offered == ["submit"] and "[NOTICE]" in fourth_messages[-1]["content"]
    final_messages, _ = backend.calls[4]
    tool_texts = tool_messages(final_messages)
    assert any(t.startswith("refused:") for t in tool_texts)
    assert any("not available at this point" in t for t in tool_texts)
    results = read_results(harness["logs"])
    assert results["baseline"]["commands_used"] == 3
    assert results["baseline"]["termination_reason"] == "budget_exhausted_forced_submit"
    assert results["baseline"]["stages"]["diagnosis"]["tool_counts"] == {"bash": 3}
    refused = [r for r in read_transcript(harness["logs"]) if r["type"] == "tool_call" and r["refused"]]
    assert len(refused) == 1 and refused[0]["refused"] == "external_submit"


def test_parallel_tool_calls_run_in_order(monkeypatch, harness):
    backend = FakeBackend(
        [
            tool_reply(call("bash", command="kubectl get pods"), call("bash", command="kubectl get svc")),
            submit(DIAGNOSIS),
        ]
    )
    assert run_main(monkeypatch, backend) == 0
    assert harness["commands"] == ["kubectl get pods", "kubectl get svc"]
    messages, _ = backend.calls[1]
    assert [m["role"] for m in messages[-3:]] == ["assistant", "tool", "tool"]
    assert (
        messages[-3]["tool_calls"][0]["function"]["name"] == "bash" and messages[-3]["reasoning_content"] == "thinking"
    )
    assert all(t.startswith("exit_code: 0") and "pod-a Running" in t for t in tool_messages(messages))


def test_unlimited_budget_is_bounded_by_the_hard_cap(monkeypatch, harness):
    backend = FakeBackend([bash("a"), bash("b"), bash("c"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, hard_cap=2) == 0
    assert harness["commands"] == ["a", "b"]
    assert backend.calls[2][1] == ["submit"]
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "hard_cap_forced_submit"


def test_voluntary_submit_keeps_its_own_reason_and_writes_steps(monkeypatch, harness):
    backend = FakeBackend([bash("kubectl get pods"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    results = read_results(harness["logs"])
    assert results["baseline"]["termination_reason"] == "submitted" and results["baseline"]["commands_used"] == 1
    assert (harness["logs"] / "steps" / "step_02" / "messages.json").exists()
    records = read_transcript(harness["logs"])
    model_calls = [r for r in records if r["type"] == "model_call"]
    assert model_calls[0]["tools_offered"] == ["bash", "submit"] and model_calls[0]["tool_calls"][0]["name"] == "bash"
    assert [r["tool"] for r in records if r["type"] == "tool_call"] == ["bash"]


def test_no_mechanism_mode_composes_text_and_rejects_once(monkeypatch, harness):
    backend = FakeBackend(
        [
            submit_nomech("service/frontend", ["search"], "requests fail because the selector is wrong"),
            submit_nomech("service/frontend", ["search"], "requests to the frontend fail intermittently"),
        ]
    )
    assert run_main(monkeypatch, backend, max_calls="3", mode="no_mechanism") == 0
    second_messages, offered = backend.calls[1]
    assert offered == ["bash", "submit"]
    assert tool_messages(second_messages)[-1].startswith("rejected: the submission explained a mechanism")
    assert harness["submissions"] == [
        (
            "Faulty component: service/frontend. Affected components: search. Observed symptom: requests to the frontend fail intermittently.",
            "diagnosis",
        )
    ]
    assert read_results(harness["logs"])["baseline"]["mechanism_guard_tripped"] is False


def test_no_mechanism_mode_submits_with_flag_after_second_violation(monkeypatch, harness):
    leaky = submit_nomech("service/frontend", [], "fails because the selector is wrong")
    backend = FakeBackend([leaky, submit_nomech("service/frontend", [], "fails because the selector is wrong")])
    assert run_main(monkeypatch, backend, mode="no_mechanism") == 0
    assert "because" in harness["submissions"][0][0]
    assert read_results(harness["logs"])["baseline"]["mechanism_guard_tripped"] is True


def test_invalid_submit_arguments_are_rejected(monkeypatch, harness):
    backend = FakeBackend(
        [tool_reply(ToolCall("c1", "submit", "{not json")), tool_reply(call("submit", diagnosis="")), submit(DIAGNOSIS)]
    )
    assert run_main(monkeypatch, backend) == 0
    messages, _ = backend.calls[2]
    texts = tool_messages(messages)
    assert (
        texts[0].startswith("rejected: tool arguments are not valid JSON")
        and "diagnosis must be a non-empty string" in texts[1]
    )
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis")]


def test_two_bad_replies_submit_the_fallback_and_exit_2(monkeypatch, harness):
    backend = FakeBackend([Reply(error="model call failed: boom"), Reply(error="model call failed: boom")])
    assert run_main(monkeypatch, backend) == 2
    assert harness["submissions"] == [(protocol.FALLBACK_DIAGNOSIS, "diagnosis")]
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "model_failure"


def test_unusable_reply_with_content_is_kept_and_explained(monkeypatch, harness):
    backend = FakeBackend(
        [Reply(content="half", tool_calls=[call("bash", command="x")], error="empty model reply"), submit(DIAGNOSIS)]
    )
    assert run_main(monkeypatch, backend) == 0
    messages, _ = backend.calls[1]
    assert [m["role"] for m in messages[-3:]] == ["assistant", "tool", "user"]
    assert messages[-2]["content"].startswith("not executed") and "could not be used" in messages[-1]["content"]
    assert harness["commands"] == []


def test_plain_text_replies_are_nudged_then_taken_as_the_answer(monkeypatch, harness):
    backend = FakeBackend(
        [
            text_reply("thinking aloud"),
            text_reply("still no tool"),
            text_reply("one more"),
            text_reply("The frontend selector is wrong."),
        ]
    )
    assert run_main(monkeypatch, backend) == 0
    assert backend.calls[1][0][-1]["content"] == protocol.NUDGE_TEXT
    assert backend.calls[3][1] == ["submit"] and "no tool call" in backend.calls[3][0][-1]["content"]
    assert harness["submissions"] == [("The frontend selector is wrong.", "diagnosis")]
    results = read_results(harness["logs"])
    assert results["baseline"]["termination_reason"] == "no_tool_call_forced_submit"
    assert results["baseline"]["stages"]["diagnosis"]["plain_text_submission"] is True


def test_mitigation_stage_continues_the_conversation(monkeypatch, harness):
    harness["stages"] = ["diagnosis", "mitigation", "done"]
    backend = FakeBackend(
        [
            bash("kubectl get pods"),
            submit(DIAGNOSIS),
            bash("kubectl patch svc frontend -n hotel-reservation -p '{}'"),
            tool_reply(call("submit", note="fixed")),
        ]
    )
    assert run_main(monkeypatch, backend) == 0
    assert harness["submissions"] == [(DIAGNOSIS, "diagnosis"), ("", "mitigation")]
    assert harness["commands"] == ["kubectl get pods", "kubectl patch svc frontend -n hotel-reservation -p '{}'"]
    mitigation_messages, offered = backend.calls[2]
    assert offered == ["bash", "submit"]
    assert mitigation_messages[: len(backend.calls[1][0])] == backend.calls[1][0]
    assert mitigation_messages[-2]["content"] == "Submission received."
    assert mitigation_messages[-1]["content"].startswith("[STAGE CHANGE]")
    results = read_results(harness["logs"])
    assert results["baseline"]["submitted_stages"] == ["diagnosis", "mitigation"]
    assert results["baseline"]["stages"]["diagnosis"]["commands_used"] == 1
    assert results["baseline"]["stages"]["mitigation"]["commands_used"] == 1
    assert results["baseline"]["stages"]["mitigation"]["termination_reason"] == "submitted"
    assert results["usage_metrics"]["input_tokens"] == 400
    assert (harness["logs"] / "steps" / "step_04").exists()


def test_attempt_starting_at_mitigation_runs_only_the_mitigation_loop(monkeypatch, harness):
    harness["stages"] = ["mitigation", "done"]
    backend = FakeBackend([tool_reply(call("submit"))])
    assert run_main(monkeypatch, backend, max_calls="3") == 0
    assert len(backend.calls) == 1 and "Current stage: mitigation" in backend.calls[0][0][1]["content"]
    assert harness["submissions"] == [("", "mitigation")]
    assert "diagnosis" not in read_results(harness["logs"])["baseline"]["stages"]


def test_external_submission_is_detected_and_not_resubmitted(monkeypatch, harness):
    monkeypatch.setattr(driver, "current_stage", lambda: "tearing_down")
    backend = FakeBackend([bash("python3 -c 'import requests'"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend, max_calls="3") == 3
    assert harness["submissions"] == []
    assert read_results(harness["logs"])["baseline"]["termination_reason"] == "external_submission"


def test_mcp_tools_are_offered_and_dispatched(monkeypatch, harness):
    specs = [ToolSpec("get_alerts", "Firing alerts.", {"type": "object", "properties": {}}, "mcp", "prometheus")]
    mcp = FakeMcp(specs)
    harness["toolbox"] = Toolbox(local=["bash"], mcp=mcp, command_timeout=5)
    backend = FakeBackend([tool_reply(call("get_alerts")), tool_reply(call("nope", x=1)), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    assert backend.calls[0][1] == ["bash", "get_alerts", "submit"]
    assert mcp.calls == [("get_alerts", {})]
    assert tool_messages(backend.calls[1][0]) == ["get_alerts result"]
    assert tool_messages(backend.calls[2][0])[-1].startswith("error: tool 'nope' is not available")
    results = read_results(harness["logs"])
    assert results["baseline"]["commands_used"] == 1 and results["baseline"]["stages"]["diagnosis"]["tool_counts"] == {
        "get_alerts": 1
    }
    assert results["baseline"]["tools"]["mcp_tools"] == ["get_alerts"]


def test_no_real_problem_id_reaches_the_logs(monkeypatch, harness):
    monkeypatch.setenv("SREGYM_ARTIFACT_ID", "anon_test")
    backend = FakeBackend([bash("kubectl get pods"), submit(DIAGNOSIS)])
    assert run_main(monkeypatch, backend) == 0
    assert _find_token_hit(harness["logs"], REAL_PROBLEM_ID) is None


def test_parse_budget():
    assert driver.parse_budget("0") == 0
    assert driver.parse_budget("10") == 10
    assert driver.parse_budget("unlimited") is None
    with pytest.raises(ValueError):
        driver.parse_budget("-1")


def test_is_external_submit():
    assert driver.is_external_submit("curl -X POST http://localhost:8000/submit -d '{}'")
    assert driver.is_external_submit("python3 -c \"requests.post('http://h:8000/submit_mcp')\"")
    assert not driver.is_external_submit("kubectl get pods -n submitter")


def test_run_command_captures_output_and_exit_code(tmp_path):
    result = tools.run_command("echo hi; echo err 1>&2; exit 3", timeout=5, cwd=str(tmp_path))
    assert result.stdout == "hi\n" and result.stderr == "err\n" and result.exit_code == 3
    slow = tools.run_command("sleep 5", timeout=1)
    assert slow.exit_code == 124 and slow.timed_out


def test_local_tools_read_and_write(tmp_path):
    text, record = tools.run_local_tool(
        "write_file", {"path": str(tmp_path / "a" / "b.yaml"), "content": "kind: Pod"}, timeout=5, cwd=None
    )
    assert text.startswith("wrote 9 chars") and record["chars"] == 9
    text, record = tools.run_local_tool("read_file", {"path": str(tmp_path / "a" / "b.yaml")}, timeout=5, cwd=None)
    assert text == "kind: Pod"
    text, record = tools.run_local_tool("read_file", {"path": str(tmp_path / "missing")}, timeout=5, cwd=None)
    assert text.startswith("error:") and "error" in record


def test_tool_selection_parsing():
    assert tools.parse_tool_selection(None) == (
        ["bash", "read_file", "write_file"],
        ["kubectl", "prometheus", "jaeger", "loki"],
    )
    assert tools.parse_tool_selection("bash") == (["bash"], [])
    assert tools.parse_tool_selection("bash,mcp:kubectl") == (["bash"], ["kubectl"])
    with pytest.raises(ValueError):
        tools.parse_tool_selection("mcp:nope")
    with pytest.raises(ValueError):
        tools.parse_tool_selection("edit_file")


def test_preflight_exits_zero_on_ok_reply(monkeypatch):
    class OkBackend(FakeBackend):
        pass

    monkeypatch.setattr(driver, "make_backend", lambda model, effort: OkBackend([text_reply("ok")]))
    with pytest.raises(SystemExit) as exc:
        driver.run_preflight()
    assert exc.value.code == 0
    monkeypatch.setattr(
        driver, "make_backend", lambda model, effort: OkBackend([Reply(error="model call failed: nope")])
    )
    with pytest.raises(SystemExit) as exc:
        driver.run_preflight()
    assert exc.value.code == 1
