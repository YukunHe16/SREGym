#!/usr/bin/env python3
"""Continue the frozen pilot after a pre-model CronJob context collector failure."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import difflib
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


pilot = _load("jev_multitask_pilot_context_fix", "run_jev_multitask.py")
recovery = _load("jev_multitask_archive_context_fix", "recover_jev_multitask_archive.py")
support = pilot.support
CONTEXT_PATH = "sregym/routing/context.py"
OLD_CONTEXT_SNIPPET = '''    if kind in {"Job", "CronJob"}:
        active = status.get("active") or 0
        failed = status.get("failed") or 0
        return (1 if active or failed or kind == "CronJob" else 3, -active - failed, kind, name)
'''
NEW_CONTEXT_SNIPPET = '''    if kind in {"Job", "CronJob"}:
        def count(value):
            if isinstance(value, list):
                return len(value)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0
        active = count(status.get("active"))
        failed = count(status.get("failed"))
        return (1 if active or failed or kind == "CronJob" else 3, -active - failed, kind, name)
'''


def context_amendment(batch: Path, manifest: dict) -> dict:
    frozen = batch / "configfreeze" / CONTEXT_PATH
    current = ROOT / CONTEXT_PATH
    if not frozen.is_file() or not current.is_file():
        raise ValueError("routing context source is missing")
    if support.sha(frozen) != manifest["source_hashes"].get(CONTEXT_PATH):
        raise ValueError("frozen routing context does not match the original manifest")
    old_text = frozen.read_text()
    if old_text.count(OLD_CONTEXT_SNIPPET) != 1:
        raise ValueError("frozen routing context does not contain the expected faulty block")
    expected = old_text.replace(OLD_CONTEXT_SNIPPET, NEW_CONTEXT_SNIPPET)
    current_text = current.read_text()
    if current_text != expected:
        raise ValueError("current routing context contains changes beyond the scoped CronJob fix")
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True), current_text.splitlines(keepends=True),
        fromfile=f"original/{CONTEXT_PATH}", tofile=f"amended/{CONTEXT_PATH}",
    ))
    return {
        "path": CONTEXT_PATH,
        "original_sha256": support.sha(frozen),
        "amended_sha256": support.sha(current),
        "scope": "normalize Kubernetes Job integer and CronJob active-reference-list counts before sorting",
        "failure_stage": "post-fault read-only context collection before Jev and Codex model calls",
        "diff": diff,
    }


def _completed_prefix(rows: list[dict], start: int, stop: int) -> list[dict]:
    if len(rows) != stop - start:
        raise ValueError("continuation result count differs from the expected stopped prefix")
    for row, index in zip(rows, range(start, stop)):
        if ((row.get("task"), row.get("arm")) != pilot.SCHEDULE[index]
                or row.get("index") != index + 1 or row.get("status") != "completed"):
            raise ValueError(f"effective run {index + 1} is not a completed scheduled result")
    return rows


def validate(batch: Path) -> tuple[dict, dict, dict, list[dict], dict, dict]:
    batch = batch.resolve()
    manifest = json.loads((batch / "manifest.json").read_text())
    first = manifest.get("runs", [])
    old = json.loads((batch / "continuation.json").read_text())
    second = json.loads((batch / "continuation2.json").read_text())
    if manifest.get("schedule") != [list(x) for x in pilot.SCHEDULE] or len(first) != 5:
        raise ValueError("original frozen batch shape differs")
    effective = _completed_prefix(first[:4], 0, 4)
    failed_deploy = first[4]
    if (failed_deploy.get("index") != 5 or failed_deploy.get("status") != "stopped_infrastructure"
            or failed_deploy.get("stop_reasons") != ["deploy_failed"]):
        raise ValueError("original run 5 is not the preserved pre-model deployment failure")
    if old.get("status") != "stopped_controller_error" or len(old.get("new_runs", [])) != 3:
        raise ValueError("first continuation shape differs")
    effective += _completed_prefix(old["new_runs"][:2], 4, 6)
    stopped_archive = old["new_runs"][2]
    if ((stopped_archive.get("task"), stopped_archive.get("arm")) != pilot.SCHEDULE[6]
            or stopped_archive.get("error_type") != "ValueError"
            or stopped_archive.get("error") != "Expected exactly one index digest in build output"):
        raise ValueError("run 7 is not the accepted post-score archive parser stop")
    recovered = second.get("recovered_run", {})
    if ((recovered.get("task"), recovered.get("arm")) != pilot.SCHEDULE[6]
            or recovered.get("index") != 7 or recovered.get("status") != "completed"):
        raise ValueError("run 7 recovery is missing or incomplete")
    effective.append(recovered)
    if second.get("status") != "stopped_main_error" or len(second.get("new_runs", [])) != 4:
        raise ValueError("second continuation is not stopped at run 11")
    effective += _completed_prefix(second["new_runs"][:3], 7, 10)
    failed_routing = second["new_runs"][3]
    row = (failed_routing.get("result_rows") or [{}])[0]
    if ((failed_routing.get("task"), failed_routing.get("arm")) != pilot.SCHEDULE[10]
            or failed_routing.get("index") != 11
            or failed_routing.get("status") != "stopped_main_error"
            or failed_routing.get("returncode") != 1
            or len(failed_routing.get("result_rows", [])) != 1
            or str(row.get("routing_failed")).lower() != "true"
            or row.get("routing_error_code") != "TypeError"
            or row.get("incomplete_reason") != "model_routing_failed"):
        raise ValueError("run 11 is not the accepted pre-model routing context failure")
    failed_dir = batch / "11-cronjob_sidecar_blocks_completion_hotel_reservation--jev"
    if list(failed_dir.rglob("routing.json")) or list(failed_dir.rglob("codex_results_*.json")):
        raise ValueError("failed run 11 unexpectedly contains Jev or Codex execution evidence")
    if (batch / "completion.json").exists() or (batch / "continuation3.json").exists():
        raise ValueError("context-fix continuation already exists")
    amendment = context_amendment(batch, manifest)
    for name, digest in manifest["source_hashes"].items():
        if name == CONTEXT_PATH:
            continue
        path = ROOT / name
        if not path.is_file() or support.sha(path) != digest:
            raise ValueError("frozen source changed beyond the recorded context fix: " + name)
    if support.minute_directories():
        raise ValueError("unarchived main.py result directory exists")
    if len(effective) != 10:
        raise ValueError("expected ten valid model results before rerunning run 11")
    return manifest, old, second, effective, failed_routing, amendment


def continue_batch(batch: Path) -> int:
    batch = batch.resolve()
    with (ROOT / "results/.jev-multitask.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another multitask controller is running") from None
        manifest, old, second, effective, failed_routing, amendment = validate(batch)
        env = support.clean_environment(os.environ)
        if support.container_ids(env):
            raise ValueError("benchmark containers are still running before continuation")
        key = getpass.getpass("TypeSafe API key for final multitask runs (not saved): ")
        if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError("invalid TypeSafe key")
        source = Path(__file__).resolve()
        snapshot = batch / "continuation3_source" / source.name
        snapshot.parent.mkdir()
        snapshot.write_bytes(source.read_bytes())
        diff_path = batch / "continuation3_source" / "context-amendment.diff"
        diff_path.write_text(amendment.pop("diff"))
        amendment["diff_artifact"] = str(diff_path.relative_to(batch))
        amendment["diff_sha256"] = support.sha(diff_path)
        state = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": "run 11 stopped before Jev and Codex because CronJob status.active is a reference list",
            "previous_continuation_sha256": support.sha(batch / "continuation2.json"),
            "failed_run_preserved": "11-cronjob_sidecar_blocks_completion_hotel_reservation--jev",
            "failed_run_included_in_comparison": False,
            "source_amendment": amendment,
            "source_snapshot": str(snapshot.relative_to(batch)),
            "source_sha256": support.sha(snapshot),
            "remaining_schedule": [list(x) for x in pilot.SCHEDULE[10:]],
            "status": "running", "new_runs": [],
        }
        support.save_json(batch / "continuation3.json", state)
        amended_runtime = None
        for schedule_index, (task, arm) in enumerate(pilot.SCHEDULE[10:], start=11):
            prefix = "11b" if schedule_index == 11 else f"{schedule_index:02d}"
            run_dir = batch / f"{prefix}-{task}--{arm}"
            run_dir.mkdir()
            summary = {
                "index": schedule_index, "task": task, "arm": arm,
                "argv": pilot.command_for(task, arm),
                "configured_model": pilot.MODELS.get(arm, pilot.MODELS["terra"]),
                "continuation3_run": True,
                "source_amendment_sha256": amendment["amended_sha256"],
            }
            try:
                current_amendment = context_amendment(batch, manifest)
                if current_amendment["amended_sha256"] != amendment["amended_sha256"]:
                    raise RuntimeError("scoped context amendment changed before continuation run")
                for name, digest in manifest["source_hashes"].items():
                    if name == CONTEXT_PATH:
                        continue
                    if not (ROOT / name).is_file() or support.sha(ROOT / name) != digest:
                        raise RuntimeError("frozen source changed beyond the recorded context fix")
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
                summary.update(
                    returncode=code, main_wall_seconds=elapsed, interrupted=interrupted,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    image_id=support.image_identity(env),
                    build_digests=recovery.build_digests(run_dir / "stdout.log"),
                )
                created = sorted(support.minute_directories())
                if len(created) != 1:
                    raise RuntimeError("expected exactly one new main result directory")
                destination = run_dir / "benchmark-results"
                created[0].rename(destination)
                csv_path = destination / "codex_ALL_results.csv"
                summary.update(
                    benchmark_results=str(destination.relative_to(batch)),
                    results_csv=str(csv_path.relative_to(batch)),
                )
                with csv_path.open(newline="") as handle:
                    summary["result_rows"] = list(csv.DictReader(handle))
                summary["status"], summary["stop_reasons"] = pilot.classify(
                    task, code, summary["result_rows"])
                for path in sorted(set((ROOT / "logs").glob("judge-codex-*")) - judge_before):
                    saved = run_dir / "judge-logs" / path.name
                    saved.parent.mkdir(exist_ok=True)
                    path.rename(saved)
                leftovers = sorted(support.container_ids(env) - before)
                summary["unexpected_running_benchmark_containers"] = leftovers
                if leftovers:
                    summary.update(status="stopped_cleanup_uncertain",
                                   stop_reasons=["benchmark_containers_still_running"])
                runtime = support.runtime_identity(summary["build_digests"])
                amended_runtime = amended_runtime or runtime
                if runtime != amended_runtime:
                    summary.update(status="stopped_infrastructure",
                                   stop_reasons=["amended_runtime_image_identity_changed"])
                if summary["image_id"] != summary["build_digests"]["index"]:
                    summary.update(status="stopped_infrastructure",
                                   stop_reasons=["image_index_record_mismatch"])
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__,
                               error=str(exc).replace(key, "[REDACTED]"))
            support.save_json(run_dir / "result.json", summary)
            state["new_runs"].append(summary)
            state["status"] = "running" if summary["status"] == "completed" else summary["status"]
            support.save_json(batch / "continuation3.json", state)
            if summary["status"] != "completed":
                print(f"Third continuation stopped after run {schedule_index}: {summary['status']}", flush=True)
                return 1
            effective.append(summary)
        if len(effective) != len(pilot.SCHEDULE):
            raise RuntimeError("effective result set is not the full 16-run schedule")
        for index, row in enumerate(effective):
            if ((row.get("task"), row.get("arm")) != pilot.SCHEDULE[index]
                    or row.get("index") != index + 1 or row.get("status") != "completed"):
                raise RuntimeError("effective result set is not aligned to the frozen schedule")
        state["status"] = "completed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        support.save_json(batch / "continuation3.json", state)
        support.save_json(batch / "completion.json", {
            "comparison_complete": True,
            "runs": effective,
            "excluded_infrastructure_attempts": [manifest["runs"][4], failed_routing],
            "controller_recoveries": [second["recovered_run"]],
            "source_amendments": [amendment],
            "official_results_rewritten": False,
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
    except (ValueError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Context-fix continuation refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
