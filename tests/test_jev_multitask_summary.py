from scripts import summarize_jev_multitask as summary


def test_truth_parses_only_explicit_true_values():
    assert summary.truth("True") and summary.truth(1) and summary.truth("yes")
    assert not summary.truth(None) and not summary.truth("") and not summary.truth("False")


def test_markdown_exposes_timeout_bias_and_scope():
    rows = []
    for index, arm in enumerate(summary.ARMS, start=1):
        rows.append({
            "index": index, "task_label": "task", "arm": arm,
            "selected_model": "gpt-5.6-sol", "diagnosis_accuracy": 0.0,
            "diagnosis_success": False, "mitigation_success": False,
            "timed_out": arm == "jev", "TTM": None, "main_wall_seconds": 1.0,
            "agent_usage": None, "route": ({"selected_model": "gpt-5.6-sol", "confidence": 0.3,
                "probabilities": {"sol": 0.5, "terra": 0.5, "luna": 0.0},
                "context_collection_seconds": 0.4, "route_latency_seconds": 0.6,
                "estimated_cost_usd": 0.0006} if arm == "jev" else None),
        })
    aggregate = {arm: {"diagnosis_accuracy_mean": 0.0, "diagnosis_successes": 0,
                       "mitigation_successes": 0, "joint_successes": 0,
                       "TTM_mean_seconds": None, "TTM_observed_count": 0,
                       "model_timeouts": int(arm == "jev"), "agent_total_tokens_known": 0,
                       "agent_usage_observed_count": 0} for arm in summary.ARMS}
    text = summary.markdown({"runs": rows, "aggregate": aggregate,
                             "routing": {"estimated_cost_usd_total": 0.0006}, "batch": "/tmp/batch"})
    assert "幸存者偏差" in text
    assert "只覆盖 Codex CLI" in text
    assert "超时未提交" in text
