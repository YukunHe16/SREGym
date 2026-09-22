#!/usr/bin/env python3
"""Accept the fixed-budget run-14 model timeout and finish runs 15-16."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


resume3 = _load("jev_multitask_context_resume_finish", "continue_jev_multitask_after_context_fix.py")
pilot = resume3.pilot
recovery = resume3.recovery
support = pilot.support
NO_SECRET = "__NO_ROUTER_SECRET_FOR_FIXED_ARMS__"
ACCEPTED_STATUSES = {"completed", "completed_model_timeout"}


def accept_model_timeout(run: dict, reference_runtime: tuple) -> dict:
    row = (run.get("result_rows") or [{}])[0]
    if ((run.get("task"), run.get("arm")) != pilot.SCHEDULE[13]
            or run.get("index") != 14 or run.get("returncode") != 0
            or run.get("status") != "stopped_infrastructure"
            or run.get("stop_reasons") != ["image_index_record_mismatch"]
            or len(run.get("result_rows", [])) != 1
            or row.get("run_status") != "incomplete"
            or row.get("incomplete_reason") != "agent_timeout"
            or row.get("incomplete_stage") != "mitigation"
            or str(row.get("timed_out")).lower() != "true"
            or row.get("Diagnosis.success") != "False"
            or row.get("Mitigation.success") not in {None, ""}):
        raise ValueError("run 14 is not the accepted fixed-budget model timeout")
    if support.runtime_identity(run["build_digests"]) != reference_runtime:
        raise ValueError("run 14 runtime bytes differ from the amended-run reference")
    accepted = dict(run)
    accepted.update(
        status="completed_model_timeout",
        stop_reasons=["agent_timeout_after_scored_diagnosis", "mitigation_not_submitted"],
        comparison_diagnosis_success=False,
        comparison_mitigation_success=False,
        comparison_policy="fixed 900-second model timeout counts as a failed model outcome; no rerun",
        original_controller_status=run["status"],
        original_controller_stop_reasons=run["stop_reasons"],
        image_index_metadata_warning={
            "local_tag_after_main": run.get("image_id"),
            "recorded_build_index": run["build_digests"].get("index"),
            "runtime_config_and_manifest_match_reference": True,
        },
    )
    return accepted


def classify_model_outcome(task: str, returncode: int, rows: list[dict]) -> tuple[str, list[str]]:
    if returncode == 0 and len(rows) == 1:
        row = rows[0]
        if (row.get("problem_id") == task and row.get("attempt") == "1"
                and row.get("run_status") == "incomplete"
                and row.get("incomplete_reason") == "agent_timeout"
                and str(row.get("timed_out")).lower() == "true"):
            infra = [name for name in ("deploy_failed", "cleanup_failed", "cleanup_timed_out",
                                       "artifact_finalization_failed", "routing_failed")
                     if str(row.get(name)).lower() in {"true", "1", "yes"}]
            if not infra:
                return "completed_model_timeout", ["agent_timeout"]
    return pilot.classify(task, returncode, rows)


def _prefix(rows: list[dict], start: int, stop: int) -> list[dict]:
    if len(rows) != stop - start:
        raise ValueError("effective result prefix has the wrong length")
    for row, offset in zip(rows, range(start, stop)):
        if ((row.get("task"), row.get("arm")) != pilot.SCHEDULE[offset]
                or row.get("index") != offset + 1 or row.get("status") != "completed"):
            raise ValueError(f"run {offset + 1} is not a completed scheduled result")
    return rows


def validate(batch: Path) -> tuple[dict, dict, dict, dict, list[dict], dict, dict]:
    batch = batch.resolve()
    manifest = json.loads((batch / "manifest.json").read_text())
    first = manifest.get("runs", [])
    old = json.loads((batch / "continuation.json").read_text())
    second = json.loads((batch / "continuation2.json").read_text())
    third = json.loads((batch / "continuation3.json").read_text())
    if manifest.get("schedule") != [list(x) for x in pilot.SCHEDULE] or len(first) != 5:
        raise ValueError("original batch shape differs")
    effective = _prefix(first[:4], 0, 4)
    if old.get("status") != "stopped_controller_error":
        raise ValueError("first continuation shape differs")
    effective += _prefix(old.get("new_runs", [])[:2], 4, 6)
    recovered7 = second.get("recovered_run", {})
    if ((recovered7.get("task"), recovered7.get("arm")) != pilot.SCHEDULE[6]
            or recovered7.get("index") != 7 or recovered7.get("status") != "completed"):
        raise ValueError("run 7 recovery differs")
    effective.append(recovered7)
    effective += _prefix(second.get("new_runs", [])[:3], 7, 10)
    if third.get("status") != "stopped_infrastructure" or len(third.get("new_runs", [])) != 4:
        raise ValueError("third continuation is not stopped after run 14")
    effective += _prefix(third["new_runs"][:3], 10, 13)
    reference_runtime = support.runtime_identity(third["new_runs"][0]["build_digests"])
    accepted14 = accept_model_timeout(third["new_runs"][3], reference_runtime)
    effective.append(accepted14)
    if (batch / "completion.json").exists() or (batch / "continuation4.json").exists():
        raise ValueError("final continuation already exists")
    amendment = resume3.context_amendment(batch, manifest)
    amendment.pop("diff")
    amendment["diff_artifact"] = "continuation3_source/context-amendment.diff"
    for name, digest in manifest["source_hashes"].items():
        if name == resume3.CONTEXT_PATH:
            continue
        path = ROOT / name
        if not path.is_file() or support.sha(path) != digest:
            raise ValueError("frozen source changed beyond the recorded context fix: " + name)
    if support.minute_directories():
        raise ValueError("unarchived main.py result directory exists")
    env = support.clean_environment(os.environ)
    if support.container_ids(env):
        raise ValueError("benchmark containers remain after explicit timeout cleanup")
    if len(effective) != 14:
        raise ValueError("expected fourteen accepted scheduled outcomes")
    return manifest, old, second, third, effective, accepted14, amendment


def _reap_new_containers(before: set[str], env: dict) -> list[str]:
    leftovers = sorted(support.container_ids(env) - before)
    if not leftovers:
        return []
    result = support.capture(["docker", "stop", "-t", "10", *leftovers], env, required=False)
    if result["returncode"] != 0 or support.container_ids(env) - before:
        raise RuntimeError("could not clean new benchmark containers after main.py exit")
    return leftovers


def finish(batch: Path) -> int:
    batch = batch.resolve()
    with (ROOT / "results/.jev-multitask.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another multitask controller is running") from None
        manifest, old, second, third, effective, accepted14, amendment = validate(batch)
        env = support.clean_environment(os.environ)
        source = Path(__file__).resolve()
        snapshot = batch / "continuation4_source" / source.name
        snapshot.parent.mkdir()
        snapshot.write_bytes(source.read_bytes())
        accepted_path = batch / "14-duplicate_pvc_mounts_social_network--jev" / "accepted_timeout_result.json"
        support.save_json(accepted_path, accepted14)
        state = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "accept fixed-budget run-14 model timeout and finish the two remaining fixed arms",
            "previous_continuation_sha256": support.sha(batch / "continuation3.json"),
            "accepted_timeout_result": str(accepted_path.relative_to(batch)),
            "accepted_timeout_result_sha256": support.sha(accepted_path),
            "timeout_policy": "900-second agent timeout is a failed model outcome; no rerun",
            "pre_resume_owned_containers_reaped": [
                "sregym-judge-codex-637dba8d", "evaluation-egress-proxy-0a58b09b"
            ],
            "remaining_schedule": [list(x) for x in pilot.SCHEDULE[14:]],
            "source_snapshot": str(snapshot.relative_to(batch)),
            "source_sha256": support.sha(snapshot),
            "status": "running", "new_runs": [],
        }
        support.save_json(batch / "continuation4.json", state)
        reference_runtime = support.runtime_identity(third["new_runs"][0]["build_digests"])
        for schedule_index, (task, arm) in enumerate(pilot.SCHEDULE[14:], start=15):
            run_dir = batch / f"{schedule_index:02d}-{task}--{arm}"
            run_dir.mkdir()
            summary = {
                "index": schedule_index, "task": task, "arm": arm,
                "argv": pilot.command_for(task, arm), "configured_model": pilot.MODELS[arm],
                "continuation4_run": True,
                "source_amendment_sha256": amendment["amended_sha256"],
            }
            try:
                current = resume3.context_amendment(batch, manifest)
                if current["amended_sha256"] != amendment["amended_sha256"]:
                    raise RuntimeError("scoped context amendment changed before final run")
                for name, digest in manifest["source_hashes"].items():
                    if name == resume3.CONTEXT_PATH:
                        continue
                    if not (ROOT / name).is_file() or support.sha(ROOT / name) != digest:
                        raise RuntimeError("frozen source changed beyond the recorded context fix")
                if support.minute_directories():
                    raise RuntimeError("unarchived main.py result directory appeared")
                before = support.container_ids(env)
                judge_before = set((ROOT / "logs").glob("judge-codex-*"))
                summary["started_at"] = datetime.now(timezone.utc).isoformat()
                print(f"[{schedule_index}/{len(pilot.SCHEDULE)}] {task} / {arm}", flush=True)
                code, elapsed, interrupted = support.run_main(
                    summary["argv"], dict(env), run_dir / "stdout.log", NO_SECRET)
                summary.update(
                    returncode=code, main_wall_seconds=elapsed, interrupted=interrupted,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    image_id_after_main=support.image_identity(env),
                    build_digests=recovery.build_digests(run_dir / "stdout.log"),
                )
                created = sorted(support.minute_directories())
                if len(created) != 1:
                    raise RuntimeError("expected exactly one new main result directory")
                destination = run_dir / "benchmark-results"
                created[0].rename(destination)
                csv_path = destination / "codex_ALL_results.csv"
                summary.update(benchmark_results=str(destination.relative_to(batch)),
                               results_csv=str(csv_path.relative_to(batch)))
                with csv_path.open(newline="") as handle:
                    summary["result_rows"] = list(csv.DictReader(handle))
                summary["status"], summary["stop_reasons"] = classify_model_outcome(
                    task, code, summary["result_rows"])
                for path in sorted(set((ROOT / "logs").glob("judge-codex-*")) - judge_before):
                    saved = run_dir / "judge-logs" / path.name
                    saved.parent.mkdir(exist_ok=True)
                    path.rename(saved)
                summary["reaped_new_benchmark_containers"] = _reap_new_containers(before, env)
                runtime = support.runtime_identity(summary["build_digests"])
                if runtime != reference_runtime:
                    summary.update(status="stopped_infrastructure",
                                   stop_reasons=["amended_runtime_image_identity_changed"])
                summary["image_index_matches_local_tag_after_main"] = (
                    summary["image_id_after_main"] == summary["build_digests"]["index"])
                if not summary["image_index_matches_local_tag_after_main"]:
                    summary["metadata_warnings"] = ["mutable_local_tag_changed_after_recorded_build"]
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__, error=str(exc))
            support.save_json(run_dir / "result.json", summary)
            state["new_runs"].append(summary)
            state["status"] = "running" if summary["status"] in ACCEPTED_STATUSES else summary["status"]
            support.save_json(batch / "continuation4.json", state)
            if summary["status"] not in ACCEPTED_STATUSES:
                print(f"Final continuation stopped after run {schedule_index}: {summary['status']}", flush=True)
                return 1
            effective.append(summary)
        if len(effective) != len(pilot.SCHEDULE):
            raise RuntimeError("effective result set is not the full 16-run schedule")
        for index, row in enumerate(effective):
            if ((row.get("task"), row.get("arm")) != pilot.SCHEDULE[index]
                    or row.get("index") != index + 1 or row.get("status") not in ACCEPTED_STATUSES):
                raise RuntimeError("effective result set is not aligned to the frozen schedule")
        state["status"] = "completed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        support.save_json(batch / "continuation4.json", state)
        support.save_json(batch / "completion.json", {
            "comparison_complete": True,
            "runs": effective,
            "model_timeout_policy": "fixed 900-second timeout counts as diagnosis/mitigation failure; no rerun",
            "excluded_infrastructure_attempts": [manifest["runs"][4], second["new_runs"][3]],
            "controller_recoveries": [second["recovered_run"], accepted14],
            "source_amendments": [amendment],
            "official_results_rewritten": False,
        })
        print(f"Multitask pilot complete: {batch}", flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    try:
        return finish(args.baseline)
    except (ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Final continuation refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
