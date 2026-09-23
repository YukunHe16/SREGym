import csv
import json

import pytest

from scripts import summarize_jev_crossover as summary


def test_each_run_keeps_time_and_agent_router_tokens_separate(tmp_path):
    batch = tmp_path / "batch"
    root = batch / "run" / "benchmark-results"
    (root / "codex" / "task" / "routing_attempt1").mkdir(parents=True)
    (root / "codex" / "task" / "codex_results_task.json").write_text(json.dumps({
        "usage_metrics": {"input_tokens": 100, "cached_input_tokens": 80,
                          "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 120}
    }))
    (root / "codex" / "task" / "routing_attempt1" / "routing.json").write_text(json.dumps({
        "selected_model": "gpt-5.6-terra", "context_collection_seconds": 0.4,
        "route_latency_seconds": 0.5, "usage": {"input_tokens": 10, "output_tokens": 2},
        "estimated_cost_usd": 0.001, "confidence": 0.3,
    }))
    (batch / "manifest.json").write_text(json.dumps({
        "schedule": [["task", 1, "jev"]], "status": "running", "runs": [{
            "index": 1, "task": "task", "repetition": 1, "arm": "jev", "status": "completed",
            "benchmark_results": "run/benchmark-results", "configured_model": "gpt-5.6-terra",
            "main_wall_seconds": 600, "result_rows": [{"problem_id": "task", "Diagnosis.accuracy": "100",
                "Diagnosis.success": "True", "Mitigation.success": "True", "TTL": "120", "TTM": "240"}],
        }],
    }))
    result = summary.collect(batch)
    row = result["runs"][0]
    assert (row["ttd_seconds"], row["ttm_seconds"], row["main_wall_seconds"]) == (120, 240, 600)
    assert row["agent_total_tokens"] == 120
    assert (row["router_input_tokens"], row["router_output_tokens"]) == (10, 2)
    assert result["by_task_arm"][0]["agent_tokens_known_sum"] == 120
    summary.write(batch, result)
    with (batch / "runs.csv").open(newline="") as file:
        assert list(csv.DictReader(file))[0]["agent_total_tokens"] == "120"
    with (batch / "task_arm_summary.csv").open(newline="") as file:
        assert list(csv.DictReader(file))[0]["ttm_observed_mean_seconds"] == "240.0"


def test_timeout_missing_usage_is_unknown(tmp_path):
    batch = tmp_path / "batch"
    (batch / "run" / "benchmark-results").mkdir(parents=True)
    (batch / "manifest.json").write_text(json.dumps({
        "schedule": [["task", 1, "sol"]], "status": "running", "runs": [{
            "index": 1, "task": "task", "repetition": 1, "arm": "sol",
            "status": "completed_model_timeout", "benchmark_results": "run/benchmark-results",
            "configured_model": "gpt-5.6-sol", "main_wall_seconds": 1200,
            "result_rows": [{"problem_id": "task", "Diagnosis.success": "False",
                             "Mitigation.success": "", "timed_out": "True"}],
        }],
    }))
    row = summary.collect(batch)["runs"][0]
    assert row["timed_out"] is True
    assert row["joint_success"] is False
    assert row["agent_total_tokens"] is None
    assert row["ttm_seconds"] is None


def test_non_timeout_missing_usage_is_rejected(tmp_path):
    batch = tmp_path / "batch"
    (batch / "run" / "benchmark-results").mkdir(parents=True)
    (batch / "manifest.json").write_text(json.dumps({
        "schedule": [["task", 1, "luna"]], "status": "running", "runs": [{
            "index": 1, "task": "task", "repetition": 1, "arm": "luna", "status": "completed",
            "benchmark_results": "run/benchmark-results", "configured_model": "gpt-5.6-luna",
            "result_rows": [{"problem_id": "task", "Diagnosis.success": "True",
                             "Mitigation.success": "True"}],
        }],
    }))
    with pytest.raises(ValueError, match="agent usage"):
        summary.collect(batch)
