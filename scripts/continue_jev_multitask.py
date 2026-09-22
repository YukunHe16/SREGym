#!/usr/bin/env python3
"""Continue the frozen multitask pilot after the pre-model missing submodule failure."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import getpass
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


pilot = _load("jev_multitask_pilot", "run_jev_multitask.py")
support = pilot.support
EXPECTED_NESTED_APP_HEAD = "7d7b074714345a0c282b0be75af7a2c504b44c95"


def validate(batch: Path) -> tuple[dict, list[dict]]:
    batch = batch.resolve()
    manifest = json.loads((batch / "manifest.json").read_text())
    if manifest.get("status") != "stopped_infrastructure" or manifest.get("schedule") != [list(x) for x in pilot.SCHEDULE]:
        raise ValueError("batch is not the frozen stopped multitask pilot")
    runs = manifest.get("runs", [])
    if len(runs) != 5:
        raise ValueError("expected four complete runs and one deployment-only failure")
    for index, row in enumerate(runs[:4]):
        if (row.get("task"), row.get("arm")) != pilot.SCHEDULE[index] or row.get("status") != "completed":
            raise ValueError("first four frozen runs are not complete in schedule order")
    failed = runs[4]
    if ((failed.get("task"), failed.get("arm")) != pilot.SCHEDULE[4]
            or failed.get("status") != "stopped_infrastructure"
            or failed.get("stop_reasons") != ["deploy_failed"]
            or failed.get("returncode") != 0
            or len(failed.get("result_rows", [])) != 1
            or str(failed["result_rows"][0].get("deploy_failed")).lower() != "true"):
        raise ValueError("fifth run is not the accepted pre-model deployment failure")
    failed_dir = batch / f"05-{failed['task']}--{failed['arm']}"
    if list(failed_dir.rglob("routing.json")) or list(failed_dir.rglob("codex_results_*.json")):
        raise ValueError("failed deployment unexpectedly contains model execution evidence")
    for name, digest in manifest["source_hashes"].items():
        path = ROOT / name
        if not path.is_file() or support.sha(path) != digest:
            raise ValueError("frozen source changed: " + name)
    nested_head = support.capture(
        ["git", "-C", "SREGym-applications/astronomy-shop", "rev-parse", "HEAD"],
        support.clean_environment(os.environ),
    )["stdout"].strip()
    if nested_head != EXPECTED_NESTED_APP_HEAD:
        raise ValueError("Astronomy Shop nested submodule is not at the expected frozen revision")
    if (batch / "continuation.json").exists() or (batch / "completion.json").exists():
        raise ValueError("continuation already exists; automatic retries are forbidden")
    return manifest, runs[:4]


def continue_batch(batch: Path) -> int:
    batch = batch.resolve()
    with (ROOT / "results/.jev-multitask.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another multitask controller is running") from None
        manifest, effective = validate(batch)
        if support.minute_directories():
            raise ValueError("unarchived main.py result directory exists")
        env = support.clean_environment(os.environ)
        key = getpass.getpass("TypeSafe API key for remaining multitask runs (not saved): ")
        if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError("invalid TypeSafe key")
        source = Path(__file__).resolve()
        snapshot = batch / "continuation_source" / source.name
        snapshot.parent.mkdir()
        snapshot.write_bytes(source.read_bytes())
        continuation = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "Astronomy Shop nested submodule was absent; failed run stopped before model execution",
            "original_manifest_sha256": support.sha(batch / "manifest.json"),
            "failed_run_preserved": "05-secret_rotation_stale_env_credentials_astronomy_shop--luna",
            "failed_run_included_in_comparison": False, "nested_application_head": EXPECTED_NESTED_APP_HEAD,
            "remaining_schedule": [list(x) for x in pilot.SCHEDULE[4:]],
            "source_snapshot": str(snapshot.relative_to(batch)), "source_sha256": support.sha(snapshot),
            "status": "running", "new_runs": [],
        }
        support.save_json(batch / "continuation.json", continuation)
        reference_runtime = support.runtime_identity(effective[0]["build_digests"])
        for schedule_index, (task, arm) in enumerate(pilot.SCHEDULE[4:], start=5):
            prefix = "05b" if schedule_index == 5 else f"{schedule_index:02d}"
            run_dir = batch / f"{prefix}-{task}--{arm}"
            run_dir.mkdir()
            summary = {"index": schedule_index, "task": task, "arm": arm, "argv": pilot.command_for(task, arm),
                       "configured_model": pilot.MODELS.get(arm, pilot.MODELS["terra"]), "continuation_run": True}
            try:
                for name, digest in manifest["source_hashes"].items():
                    if not (ROOT / name).is_file() or support.sha(ROOT / name) != digest:
                        raise RuntimeError("frozen source changed before continuation run")
                if support.minute_directories():
                    raise RuntimeError("unarchived main.py result directory appeared")
                before = support.container_ids(env)
                judge_before = set((ROOT / "logs").glob("judge-codex-*"))
                child_env = dict(env)
                if arm == "jev":
                    child_env["TYPESAFE_API_KEY"] = key
                summary["started_at"] = datetime.now(timezone.utc).isoformat()
                print(f"[{schedule_index}/{len(pilot.SCHEDULE)}] {task} / {arm}", flush=True)
                code, elapsed, interrupted = support.run_main(
                    summary["argv"], child_env, run_dir / "stdout.log", key)
                child_env.pop("TYPESAFE_API_KEY", None)
                summary.update(returncode=code, main_wall_seconds=elapsed, interrupted=interrupted,
                               finished_at=datetime.now(timezone.utc).isoformat(), image_id=support.image_identity(env),
                               build_digests=support.build_digests(run_dir / "stdout.log"))
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
                summary["status"], summary["stop_reasons"] = pilot.classify(task, code, summary["result_rows"])
                for path in sorted(set((ROOT / "logs").glob("judge-codex-*")) - judge_before):
                    saved = run_dir / "judge-logs" / path.name
                    saved.parent.mkdir(exist_ok=True)
                    path.rename(saved)
                leftovers = sorted(support.container_ids(env) - before)
                summary["unexpected_running_benchmark_containers"] = leftovers
                if leftovers:
                    summary.update(status="stopped_cleanup_uncertain", stop_reasons=["benchmark_containers_still_running"])
                if support.runtime_identity(summary["build_digests"]) != reference_runtime:
                    summary.update(status="stopped_infrastructure", stop_reasons=["runtime_image_identity_changed"])
                if summary["image_id"] != summary["build_digests"]["index"]:
                    summary.update(status="stopped_infrastructure", stop_reasons=["image_index_record_mismatch"])
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__,
                               error=str(exc).replace(key, "[REDACTED]"))
            support.save_json(run_dir / "result.json", summary)
            continuation["new_runs"].append(summary)
            continuation["status"] = "running" if summary["status"] == "completed" else summary["status"]
            support.save_json(batch / "continuation.json", continuation)
            if summary["status"] != "completed":
                print(f"Continuation stopped after run {schedule_index}: {summary['status']}", flush=True)
                return 1
            effective.append(summary)
        continuation["status"] = "completed"
        continuation["completed_at"] = datetime.now(timezone.utc).isoformat()
        support.save_json(batch / "continuation.json", continuation)
        support.save_json(batch / "completion.json", {
            "comparison_complete": len(effective) == len(pilot.SCHEDULE), "runs": effective,
            "excluded_infrastructure_attempts": [manifest["runs"][4]], "official_results_rewritten": False,
        })
        print(f"Multitask pilot complete: {batch}", flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--prompt-key", action="store_true", required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("--prompt-key requires an interactive terminal")
    try:
        return continue_batch(args.baseline)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Continuation refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
