from scripts import finish_jev_multitask_after_model_timeout as finish


def test_timeout_is_accepted_as_failed_model_outcome():
    task, arm = finish.pilot.SCHEDULE[13]
    run = {
        "index": 14, "task": task, "arm": arm, "returncode": 0,
        "status": "stopped_infrastructure", "stop_reasons": ["image_index_record_mismatch"],
        "image_id": "index-after", "build_digests": {
            "config": "config", "runtime_manifest": "runtime", "index": "index-build",
        },
        "result_rows": [{
            "run_status": "incomplete", "incomplete_reason": "agent_timeout",
            "incomplete_stage": "mitigation", "timed_out": "True",
            "Diagnosis.success": "False", "Mitigation.success": "",
        }],
    }
    got = finish.accept_model_timeout(run, ("config", "runtime"))
    assert got["status"] == "completed_model_timeout"
    assert got["comparison_diagnosis_success"] is False
    assert got["comparison_mitigation_success"] is False


def test_classifier_accepts_clean_agent_timeout_without_retry():
    task, _ = finish.pilot.SCHEDULE[14]
    rows = [{"problem_id": task, "attempt": "1", "run_status": "incomplete",
             "incomplete_reason": "agent_timeout", "timed_out": "True"}]
    assert finish.classify_model_outcome(task, 0, rows) == ("completed_model_timeout", ["agent_timeout"])
    rows[0]["cleanup_failed"] = "True"
    status, reasons = finish.classify_model_outcome(task, 0, rows)
    assert status == "stopped_infrastructure" and reasons == ["cleanup_failed"]
