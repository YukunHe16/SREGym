"""Task-level router wiring without Kubernetes, containers, or live model calls."""

import csv
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sregym.phases import PhaseLedger, read_ledger
from sregym.routing import jev


@pytest.fixture
def benchmark(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_ID", "fixture-model")
    monkeypatch.setenv("JUDGE_MODEL_ID", "fixture-judge")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "medium")
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("jev_integration_main", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def configuration(**overrides):
    return SimpleNamespace(**{
        "agent": "codex", "model": "configured-model", "judge_model": "fixed-judge",
        "model_router": "jev", "use_external_harness": False, "reasoning_effort": "medium",
        **overrides,
    })


def decision(model="gpt-5.6-luna"):
    return {"selected_model": model, "confidence": 0.8, "probabilities": {"luna": 0.8, "sol": 0.2},
            "route_latency_seconds": 0.15, "usage": {"input_tokens": 100, "output_tokens": 20},
            "estimated_cost_usd": 0.0000042}


def test_default_mode_preserves_fixed_models_without_validating_router(benchmark, monkeypatch):
    validate = Mock(side_effect=AssertionError("disabled router must not validate"))
    monkeypatch.setattr(jev, "validate_configuration", validate)
    args = configuration()
    del args.model_router

    assert benchmark._configure_model_environment(args) == ("configured-model", "fixed-judge")
    assert os.environ["AGENT_MODEL_ID"] == "configured-model"
    assert os.environ["JUDGE_MODEL_ID"] == "fixed-judge"
    validate.assert_not_called()


@pytest.mark.parametrize("overrides,message", [
    ({"agent": "claudecode"}, "requires --agent codex"),
    ({"use_external_harness": True}, "without --use-external-harness"),
    ({"judge_model": None}, "explicit --judge-model"),
    ({"model_router": "unknown"}, "must be none or jev"),
])
def test_invalid_router_configuration_is_rejected_before_env_changes(benchmark, monkeypatch, overrides, message):
    monkeypatch.setenv("AGENT_MODEL_ID", "before-agent")
    monkeypatch.setenv("JUDGE_MODEL_ID", "before-judge")
    validate = Mock()
    monkeypatch.setattr(jev, "validate_configuration", validate)

    with pytest.raises(ValueError, match=message):
        benchmark._configure_model_environment(configuration(**overrides))

    validate.assert_not_called()
    assert os.environ["AGENT_MODEL_ID"] == "before-agent"
    assert os.environ["JUDGE_MODEL_ID"] == "before-judge"


def test_router_configuration_validation_precedes_model_changes(benchmark, monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_ID", "before-agent")
    monkeypatch.setenv("JUDGE_MODEL_ID", "before-judge")
    validate = Mock(side_effect=jev.JevRoutingError("missing_key"))
    monkeypatch.setattr(jev, "validate_configuration", validate)

    with pytest.raises(jev.JevRoutingError):
        benchmark._configure_model_environment(configuration())

    validate.assert_called_once_with()
    assert os.environ["AGENT_MODEL_ID"] == "before-agent"
    assert os.environ["JUDGE_MODEL_ID"] == "before-judge"


def test_valid_router_configuration_keeps_explicit_judge(benchmark, monkeypatch):
    validate = Mock()
    monkeypatch.setattr(jev, "validate_configuration", validate)

    assert benchmark._configure_model_environment(configuration()) == ("configured-model", "fixed-judge")

    validate.assert_called_once_with()
    assert os.environ["AGENT_MODEL_ID"] == "configured-model"
    assert os.environ["JUDGE_MODEL_ID"] == "fixed-judge"


@pytest.mark.parametrize("previous", [None, "configured-model"])
def test_selected_model_restores_environment_even_if_launch_raises(benchmark, monkeypatch, previous):
    if previous is None:
        monkeypatch.delenv("AGENT_MODEL_ID", raising=False)
    else:
        monkeypatch.setenv("AGENT_MODEL_ID", previous)
    monkeypatch.setenv("JUDGE_MODEL_ID", "fixed-judge")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "medium")
    monkeypatch.setenv("AGENT_API_BASE", "https://provider.example.test/v1")

    with pytest.raises(RuntimeError, match="launch failed"):
        with benchmark._selected_agent_model("gpt-5.6-sol"):
            assert os.environ["AGENT_MODEL_ID"] == "gpt-5.6-sol"
            assert os.environ["JUDGE_MODEL_ID"] == "fixed-judge"
            raise RuntimeError("launch failed")

    assert os.environ.get("AGENT_MODEL_ID") == previous
    assert os.environ["JUDGE_MODEL_ID"] == "fixed-judge"
    assert os.environ["AGENT_REASONING_EFFORT"] == "medium"
    assert os.environ["AGENT_API_BASE"] == "https://provider.example.test/v1"


class FakeConductor:
    def __init__(self, benchmark):
        self.problems = SimpleNamespace(get_problem_ids=Mock(return_value=["private_fault_id"]))
        self.app = SimpleNamespace(app_name="public-app", namespace="public-namespace", description="Public description",
                                   expected_fault="DO_NOT_SEND", oracle="DO_NOT_SEND")
        self.results = {}
        self.stage_sequence = [{"name": "diagnosis"}]
        self.submission_stage = "diagnosis"
        self.phases = None
        self.register_agent = Mock()
        self.start_k8s_proxy = Mock()
        self.stop_k8s_proxy = Mock()
        self.get_agent_kubeconfig_path = Mock(return_value="test-kubeconfig")
        self.start_problem = AsyncMock(side_effect=self.start)
        self.finish_problem_in_background = Mock(side_effect=self.finish)
        self.wait_for_submission_work = AsyncMock()
        self.abandon_submission_work = Mock()
        self.close_submissions = Mock(return_value=False)
        self.benchmark = benchmark

    def bind_phase_ledger(self, path, **context):
        self.phases = PhaseLedger(path, context={"problem_id": self.problem_id, **context})

    async def start(self):
        self.results = {}
        self.submission_stage = "diagnosis"
        self.phases.record("deploy", "start")
        self.phases.record("deploy", "end", outcome="ok")
        return self.benchmark.StartProblemResult.SUCCESS

    def finish(self):
        self.phases.record("cleanup", "start")
        self.submission_stage = "done"
        self.phases.record("cleanup", "end", outcome="ok")

    def record_incomplete_attempt(self, reason, **_kwargs):
        self.results.update(run_status="incomplete", incomplete_reason=reason)

    def finalize_attempt_status(self):
        return self.results.setdefault("run_status", "complete")


@pytest.fixture
def driver(benchmark, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_MODEL_ID", "configured-model")
    monkeypatch.setenv("JUDGE_MODEL_ID", "fixed-judge")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "medium")
    monkeypatch.setattr(benchmark.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark, "get_current_datetime_formatted", lambda: "test-batch")
    monkeypatch.setattr(benchmark, "get_profile", lambda: "svelte")
    monkeypatch.setattr(benchmark, "list_agents", lambda **_kwargs: {"codex": {}})
    monkeypatch.setattr(benchmark, "get_agent", lambda *_args, **_kwargs: SimpleNamespace(name="codex"))
    monkeypatch.setattr(benchmark.trace_postprocess, "write_trajectory", lambda _path: None)
    conductor = FakeConductor(benchmark)
    captured_models = []

    async def launch(_registration):
        captured_models.append((os.environ["AGENT_MODEL_ID"], os.environ["JUDGE_MODEL_ID"]))
        conductor.results["Diagnosis"] = {"success": True}
        conductor.finish()
        return None

    launcher = SimpleNamespace(_procs={}, _container_runner=None, set_agent_kubeconfig=Mock(),
                               ensure_started=AsyncMock(side_effect=launch), cleanup_agent=Mock(),
                               internet_policy_result=Mock(return_value={"internet_access": "filtered"}))
    monkeypatch.setattr(benchmark, "LAUNCHER", launcher)
    return conductor, launcher, captured_models


def test_routes_each_attempt_with_public_fields_and_restores_fixed_judge(benchmark, driver, monkeypatch, tmp_path):
    conductor, launcher, captured_models = driver
    decisions = [decision("gpt-5.6-luna"), decision("gpt-5.6-sol")]
    route = Mock(side_effect=decisions)
    monkeypatch.setattr(jev, "route_app", route)

    results = benchmark.driver_loop(conductor, agent_to_run="codex", n_attempts=2, model_router="jev")

    assert captured_models == [("gpt-5.6-luna", "fixed-judge"), ("gpt-5.6-sol", "fixed-judge")]
    assert os.environ["AGENT_MODEL_ID"] == "configured-model"
    assert os.environ["JUDGE_MODEL_ID"] == "fixed-judge"
    assert os.environ["AGENT_REASONING_EFFORT"] == "medium"
    assert launcher.ensure_started.await_count == 2
    assert route.call_count == 2
    for attempt, (call, row) in enumerate(zip(route.call_args_list, results[0]["codex"]), start=1):
        assert call.args == ({"app_name": "public-app", "namespace": "public-namespace", "descriptions": "Public description"},)
        output_dir = call.kwargs["output_dir"]
        assert output_dir == Path("results/test-batch/codex/private_fault_id") / f"routing_attempt{attempt}"
        assert not output_dir.resolve().is_relative_to((tmp_path / ".runtime").resolve())
        assert row["routing_model"] == decisions[attempt - 1]["selected_model"]
        assert row["routing_confidence"] == 0.8
        assert row["routing_latency_seconds"] == 0.15
        assert row["routing_estimated_cost_usd"] == 0.0000042
        ledger = read_ledger(output_dir.parent / f"phases_attempt{attempt}.jsonl")
        assert ledger[0]["model"] == "configured-model"
        selected = next(entry for entry in ledger if entry["phase"] == "model_routing" and entry["event"] == "end")
        assert selected["model"] == row["routing_model"]
        assert selected["configured_model"] == "configured-model"
        cleanup = next(entry for entry in ledger if entry["phase"] == "cleanup")
        assert cleanup["model"] == row["routing_model"]
        assert all(entry["judge_model"] == "fixed-judge" for entry in ledger)
    conductor.stop_k8s_proxy.assert_called_once()


def test_disabled_router_does_not_call_route_or_add_routing_results(benchmark, driver, monkeypatch):
    conductor, launcher, captured_models = driver
    route = Mock(side_effect=AssertionError("router is disabled"))
    monkeypatch.setattr(jev, "route_app", route)

    rows = benchmark.driver_loop(conductor, agent_to_run="codex")[0]["codex"]

    route.assert_not_called()
    assert captured_models == [("configured-model", "fixed-judge")]
    assert not any(key.startswith("routing_") for key in rows[0])
    launcher.ensure_started.assert_awaited_once()


@pytest.mark.parametrize("cleanup_timeout", [False, True])
def test_routing_failure_cleans_up_persists_partial_results_and_never_falls_back(
    benchmark, driver, monkeypatch, tmp_path, cleanup_timeout,
):
    conductor, launcher, captured_models = driver
    routing_error = jev.JevRoutingError("transport_error")
    routing_error.args = ("untrusted secret exception text",)
    route = Mock(side_effect=routing_error)
    monkeypatch.setattr(jev, "route_app", route)
    if cleanup_timeout:
        conductor.wait_for_submission_work.side_effect = TimeoutError

    with pytest.raises(benchmark.BenchmarkCampaignAborted) as error:
        benchmark.driver_loop(conductor, agent_to_run="codex", n_attempts=2, model_router="jev")

    launcher.ensure_started.assert_not_awaited()
    assert captured_models == []
    conductor.start_problem.assert_awaited_once()
    conductor.finish_problem_in_background.assert_called_once()
    conductor.wait_for_submission_work.assert_awaited_once()
    conductor.stop_k8s_proxy.assert_called_once()
    rows = error.value.partial_results[0]["codex"]
    assert len(rows) == 1
    assert rows[0]["routing_failed"] is True
    assert rows[0]["routing_error_code"] == "transport_error"
    assert rows[0]["infrastructure_failure"] == "model_routing_failed"
    assert rows[0]["run_status"] == "incomplete"
    assert "Diagnosis.success" not in rows[0]
    assert "routing_model" not in rows[0]
    assert "untrusted secret" not in json.dumps(rows)
    assert os.environ["AGENT_MODEL_ID"] == "configured-model"
    assert os.environ["JUDGE_MODEL_ID"] == "fixed-judge"
    path = tmp_path / "results/test-batch/codex/private_fault_id/private_fault_id_codex_results.csv"
    with path.open() as handle:
        saved = list(csv.DictReader(handle))
    assert saved[0]["routing_error_code"] == "transport_error"
    ledger = read_ledger(path.parent / "phases_attempt1.jsonl")
    error_event = next(entry for entry in ledger if entry["phase"] == "model_routing" and entry["event"] == "end")
    assert error_event["error_code"] == "transport_error"
    assert error_event["outcome"] == "error"
    assert "untrusted secret" not in json.dumps(ledger)
    if cleanup_timeout:
        assert rows[0]["cleanup_timed_out"] is True
        conductor.abandon_submission_work.assert_called_once()
