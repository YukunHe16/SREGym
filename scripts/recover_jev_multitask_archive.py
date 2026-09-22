#!/usr/bin/env python3
"""Recover one completed run from duplicate build-log lines, then finish the frozen pilot."""

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
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pilot = _load("jev_multitask_pilot_recovery", "run_jev_multitask.py")
support = pilot.support
CURRENT_INDEX = 7


def build_digests(log_path: Path) -> dict:
    text = log_path.read_text(errors="replace")
    found = {}
    for name, label in (("config", "config"), ("runtime_manifest", "manifest"), ("index", "manifest list")):
        values = re.findall(r"exporting " + label + r" (sha256:[0-9a-f]{64})(?=\s|$)", text)
        unique = list(dict.fromkeys(values))
        if len(unique) != 1:
            raise ValueError(f"expected one unique {name} digest, found {unique}")
        found[name] = unique[0]
        found[name + "_occurrences"] = len(values)
    found["attestations"] = list(dict.fromkeys(
        re.findall(r"exporting attestation manifest (sha256:[0-9a-f]{64})(?=\s|$)", text)
    ))
    return found


def _validate(batch: Path) -> tuple[dict, dict, list[dict]]:
    manifest = json.loads((batch / "manifest.json").read_text())
    first = manifest.get("runs", [])
    old = json.loads((batch / "continuation.json").read_text())
    if manifest.get("schedule") != [list(x) for x in pilot.SCHEDULE] or len(first) != 5:
        raise ValueError("original frozen batch shape differs")
    if old.get("status") != "stopped_controller_error" or len(old.get("new_runs", [])) != 3:
        raise ValueError("first continuation is not stopped at the archive parser")
    if [row.get("status") for row in old["new_runs"]] != ["completed", "completed", "stopped_controller_error"]:
        raise ValueError("unexpected first-continuation statuses")
    failed = old["new_runs"][-1]
    if ((failed.get("task"), failed.get("arm")) != pilot.SCHEDULE[6]
            or failed.get("error_type") != "ValueError"
            or failed.get("error") != "Expected exactly one index digest in build output"):
        raise ValueError("run 7 did not stop on the accepted duplicate digest-line parser issue")
    if (batch / "completion.json").exists() or (batch / "continuation2.json").exists():
        raise ValueError("archive recovery already exists")
    for name, digest in manifest["source_hashes"].items():
        path = ROOT / name
        if not path.is_file() or support.sha(path) != digest:
            raise ValueError("frozen source changed: " + name)
    minute = support.minute_directories()
    if len(minute) != 1:
        raise ValueError("expected one preserved completed main result directory")
    return manifest, old, [*first[:4], *old["new_runs"][:2]], minute[0]


def _archive_completed_run(batch: Path, failed: dict, minute: Path, reference_runtime: tuple) -> dict:
    run_dir = batch / f"07-{failed['task']}--{failed['arm']}"
    destination = run_dir / "benchmark-results"
    minute.rename(destination)
    csv_path = destination / "codex_ALL_results.csv"
    with csv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    status, reasons = pilot.classify(failed["task"], 0, rows)
    digests = build_digests(run_dir / "stdout.log")
    if support.runtime_identity(digests) != reference_runtime:
        raise ValueError("recovered run used different runtime image bytes")
    current_image = support.image_identity(support.clean_environment(os.environ))
    if current_image != digests["index"]:
        raise ValueError("recovered run image index no longer matches the local tag")
    recovered = {
        "index": CURRENT_INDEX, "task": failed["task"], "arm": failed["arm"],
        "configured_model": pilot.MODELS[failed["arm"]], "continuation_run": True,
        "started_at": failed.get("started_at"), "finished_at": datetime.now(timezone.utc).isoformat(),
        "returncode": 0, "returncode_provenance": "controller reached post-main build-log parsing",
        "interrupted": False, "main_wall_seconds": None,
        "image_id": current_image, "build_digests": digests,
        "benchmark_results": str(destination.relative_to(batch)), "results_csv": str(csv_path.relative_to(batch)),
        "result_rows": rows, "status": status, "stop_reasons": reasons,
        "unexpected_running_benchmark_containers": [],
        "recovered_from": str((run_dir / "result.json").relative_to(batch)),
        "recovery_reason": "identical duplicate OCI index log lines; model and official scoring were already complete",
    }
    support.save_json(run_dir / "recovered_result.json", recovered)
    return recovered


def recover_and_continue(batch: Path) -> int:
    batch = batch.resolve()
    with (ROOT / "results/.jev-multitask.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another multitask controller is running") from None
        manifest, old, effective, minute = _validate(batch)
        reference = support.runtime_identity(effective[0]["build_digests"])
        recovered = _archive_completed_run(batch, old["new_runs"][-1], minute, reference)
        if recovered["status"] != "completed":
            raise ValueError("recovered run is not a complete official model result")
        effective.append(recovered)
        env = support.clean_environment(os.environ)
        key = getpass.getpass("TypeSafe API key for remaining multitask runs (not saved): ")
        if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError("invalid TypeSafe key")
        source = Path(__file__).resolve()
        snapshot = batch / "continuation2_source" / source.name
        snapshot.parent.mkdir()
        snapshot.write_bytes(source.read_bytes())
        state = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "recover completed run 7 from identical duplicate OCI index output lines",
            "old_continuation_sha256": support.sha(batch / "continuation.json"),
            "old_run7_result_sha256": support.sha(batch / f"07-{recovered['task']}--{recovered['arm']}/result.json"),
            "recovered_run": recovered, "remaining_schedule": [list(x) for x in pilot.SCHEDULE[7:]],
            "source_snapshot": str(snapshot.relative_to(batch)), "source_sha256": support.sha(snapshot),
            "status": "running", "new_runs": [],
        }
        support.save_json(batch / "continuation2.json", state)
        # The oldest unarchived judge directory belongs to the pre-model deploy
        # failure. Preserve it in the infrastructure attempt; archive the newest
        # directory with recovered run 7.
        judge_dirs = sorted((ROOT / "logs").glob("judge-codex-*"), key=lambda path: path.stat().st_mtime)
        if judge_dirs:
            saved = batch / f"07-{recovered['task']}--{recovered['arm']}/judge-logs" / judge_dirs[-1].name
            saved.parent.mkdir(exist_ok=True)
            judge_dirs[-1].rename(saved)
        for schedule_index, (task, arm) in enumerate(pilot.SCHEDULE[7:], start=8):
            run_dir = batch / f"{schedule_index:02d}-{task}--{arm}"
            run_dir.mkdir()
            summary = {"index": schedule_index, "task": task, "arm": arm,
                       "argv": pilot.command_for(task, arm),
                       "configured_model": pilot.MODELS.get(arm, pilot.MODELS["terra"]),
                       "continuation2_run": True}
            try:
                for name, digest in manifest["source_hashes"].items():
                    if not (ROOT / name).is_file() or support.sha(ROOT / name) != digest:
                        raise RuntimeError("frozen source changed before continuation2 run")
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
                               build_digests=build_digests(run_dir / "stdout.log"))
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
                if support.runtime_identity(summary["build_digests"]) != reference:
                    summary.update(status="stopped_infrastructure", stop_reasons=["runtime_image_identity_changed"])
                if summary["image_id"] != summary["build_digests"]["index"]:
                    summary.update(status="stopped_infrastructure", stop_reasons=["image_index_record_mismatch"])
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__,
                               error=str(exc).replace(key, "[REDACTED]"))
            support.save_json(run_dir / "result.json", summary)
            state["new_runs"].append(summary)
            state["status"] = "running" if summary["status"] == "completed" else summary["status"]
            support.save_json(batch / "continuation2.json", state)
            if summary["status"] != "completed":
                print(f"Second continuation stopped after run {schedule_index}: {summary['status']}", flush=True)
                return 1
            effective.append(summary)
        state["status"] = "completed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        support.save_json(batch / "continuation2.json", state)
        support.save_json(batch / "completion.json", {
            "comparison_complete": len(effective) == len(pilot.SCHEDULE), "runs": effective,
            "excluded_infrastructure_attempts": [manifest["runs"][4]],
            "archive_recovery": recovered, "official_results_rewritten": False,
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
        return recover_and_continue(args.baseline)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Archive recovery refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
