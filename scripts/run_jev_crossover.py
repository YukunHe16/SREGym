#!/usr/bin/env python3
"""Run or deliberately pause the frozen 72-run Jev crossover follow-up."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("jev_multitask_support", ROOT / "scripts/run_jev_multitask.py")
assert SPEC and SPEC.loader
support_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support_module)
support = support_module.support
MODELS = support_module.MODELS
PROTOCOL = ROOT / "docs/jev-routing/CROSSOVER_PROTOCOL.md"
SEED = 20260922
TASKS = (
    "wrong_dns_policy_astronomy_shop",
    "internal_traffic_policy_local_astronomy_shop",
    "finalizer_deadlock_controller_hotel_reservation",
    "network_policy_block",
    "wrong_service_selector_social_network",
    "admission_webhook_outage_hotel_reservation",
)
ARMS = ("jev", "luna", "terra", "sol")
PRIOR_PATHS = (
    Path("/Users/yukun/Documents/ChatGPT/research/output/sregym-codex-lite-20260921/full/results/0921_0021/codex_ALL_results.csv"),
    Path("/Users/yukun/Documents/ChatGPT/research/output/sregym-pial-traces-20260922/raw/SREGym-lite__gpt-5.6-terra_medium/results.csv"),
    Path("/Users/yukun/Documents/ChatGPT/research/output/sregym-pial-traces-20260922/raw/SREGym-lite__gpt-5.6-sol_max/results.csv"),
)


def schedule() -> tuple[tuple[str, int, str], ...]:
    first = (TASKS[0], 1)
    blocks = [(task, repetition) for task in TASKS for repetition in range(1, 4)]
    blocks.remove(first)
    random.Random(SEED).shuffle(blocks)
    ordered = [first, *blocks]
    return tuple(
        (task, repetition, ARMS[(position + offset) % len(ARMS)])
        for position, (task, repetition) in enumerate(ordered)
        for offset in range(len(ARMS))
    )


SCHEDULE = schedule()


def command_for(task: str, arm: str) -> list[str]:
    return support_module.command_for(task, arm)


def parse_digests(log_path: Path) -> dict:
    output = log_path.read_text(errors="replace")
    result = {}
    for name, label in (("config", "config"), ("runtime_manifest", "manifest"), ("index", "manifest list")):
        values = re.findall(r"exporting " + label + r" (sha256:[0-9a-f]{64})(?=\s|$)", output)
        unique = list(dict.fromkeys(values))
        if len(unique) != 1:
            raise ValueError(f"expected one unique {name} digest, found {len(unique)}")
        result[name] = unique[0]
        result[name + "_occurrences"] = len(values)
    return result


def classify(task: str, returncode: int, rows: list[dict]) -> tuple[str, list[str]]:
    if returncode == 0 and len(rows) == 1:
        row = rows[0]
        if (row.get("problem_id") == task and row.get("attempt") == "1"
                and row.get("run_status") == "incomplete"
                and row.get("incomplete_reason") == "agent_timeout"
                and str(row.get("timed_out", "")).lower() == "true"):
            infra = [name for name in ("deploy_failed", "cleanup_failed", "cleanup_timed_out",
                                       "artifact_finalization_failed", "routing_failed")
                     if str(row.get(name, "")).lower() in {"true", "1", "yes"}]
            if not infra and not row.get("Diagnosis.error") and not row.get("Mitigation.error"):
                return "completed_model_timeout", ["fixed_budget_agent_timeout"]
    return support_module.classify(task, returncode, rows)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _freeze(batch: Path) -> dict[str, str]:
    hashes = support_module.freeze_sources(batch)
    source = Path(__file__).resolve()
    destination = batch / "configfreeze" / source.relative_to(ROOT)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    destination.write_bytes(data)
    hashes[str(source.relative_to(ROOT))] = hashlib.sha256(data).hexdigest()
    return hashes


def _validate_frozen(manifest: dict) -> None:
    for name, digest in manifest["source_hashes"].items():
        path = ROOT / name
        if not path.is_file() or support.sha(path) != digest:
            raise RuntimeError("frozen source changed: " + name)


def _archive_results(batch: Path, run_dir: Path, task: str, summary: dict) -> None:
    created = sorted(support.minute_directories())
    if len(created) != 1:
        raise RuntimeError("expected one new main.py result directory")
    destination = run_dir / "benchmark-results"
    created[0].rename(destination)
    csv_path = destination / "codex_ALL_results.csv"
    with csv_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary.update(benchmark_results=str(destination.relative_to(batch)),
                   results_csv=str(csv_path.relative_to(batch)), result_rows=rows)
    summary["status"], summary["stop_reasons"] = classify(task, summary["returncode"], rows)


def _reap_new_containers(before: set[str], env: dict) -> list[str]:
    leftovers = sorted(support.container_ids(env) - before)
    if not leftovers:
        return []
    result = support.capture(["docker", "stop", "-t", "10", *leftovers], env, required=False)
    if result["returncode"] != 0 or support.container_ids(env) - before:
        raise RuntimeError("could not clean new benchmark containers after main.py exit")
    return leftovers


def _run_one(batch: Path, manifest: dict, env: dict, key: str, index: int) -> dict:
    task, repetition, arm = SCHEDULE[index]
    run_dir = batch / f"{index + 1:03d}-{task}--r{repetition}--{arm}"
    run_dir.mkdir()
    summary = {"index": index + 1, "task": task, "repetition": repetition, "arm": arm,
               "argv": command_for(task, arm),
               "configured_model": MODELS["terra"] if arm == "jev" else MODELS[arm]}
    before = support.container_ids(env)
    judge_before = set((ROOT / "logs").glob("judge-codex-*"))
    try:
        _validate_frozen(manifest)
        if support.minute_directories():
            raise RuntimeError("unarchived main.py result directory exists")
        if before:
            raise RuntimeError("benchmark containers already running before this run")
        child_env = dict(env)
        if arm == "jev":
            child_env["TYPESAFE_API_KEY"] = key
        summary["started_at"] = _now()
        print(f"[{index + 1}/{len(SCHEDULE)}] {task} r{repetition} / {arm}", flush=True)
        code, elapsed, interrupted = support.run_main(summary["argv"], child_env, run_dir / "stdout.log", key)
        child_env.pop("TYPESAFE_API_KEY", None)
        summary.update(returncode=code, main_wall_seconds=elapsed, interrupted=interrupted,
                       finished_at=_now(), build_digests=parse_digests(run_dir / "stdout.log"),
                       image_id_after_main=support.image_identity(env))
        _archive_results(batch, run_dir, task, summary)
        reference = manifest.get("runtime_identity")
        actual = list(support.runtime_identity(summary["build_digests"]))
        if reference is None:
            manifest["runtime_identity"] = actual
        elif actual != reference:
            summary.update(status="stopped_infrastructure", stop_reasons=["runtime_image_bytes_changed"])
        summary["image_index_matches_local_tag_after_main"] = (
            summary["image_id_after_main"] == summary["build_digests"]["index"])
        if interrupted:
            summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
    except Exception as exc:
        summary.update(status="stopped_controller_error", error_type=type(exc).__name__,
                       error=str(exc).replace(key, "[REDACTED]"))
    finally:
        for path in sorted(set((ROOT / "logs").glob("judge-codex-*")) - judge_before):
            saved = run_dir / "judge-logs" / path.name
            saved.parent.mkdir(exist_ok=True)
            path.rename(saved)
        try:
            summary["reaped_new_benchmark_containers"] = _reap_new_containers(before, env)
        except Exception as exc:
            summary.update(status="stopped_cleanup_uncertain", stop_reasons=["container_cleanup_failed"],
                           cleanup_error=str(exc).replace(key, "[REDACTED]"))
        support.save_json(run_dir / "result.json", summary)
    return summary


def run(batch: Path | None, *, max_runs: int | None) -> int:
    if not support.PYTHON.is_file() or not support.KUBECONFIG.is_file():
        raise RuntimeError("fixed Python interpreter and kubeconfig are required")
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    with (results / ".jev-crossover.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another crossover controller is running") from None
        if support.minute_directories():
            raise RuntimeError("unarchived main.py result directory exists")
        env = support.clean_environment(os.environ)
        if support.container_ids(env):
            raise RuntimeError("benchmark containers already running")
        if batch is None:
            batch = results / ("jev-crossover-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
            batch.mkdir()
            manifest = {
                "created_at": _now(), "protocol": str(PROTOCOL.relative_to(ROOT)), "seed": SEED,
                "schedule": [list(item) for item in SCHEDULE], "tasks": list(TASKS), "models": MODELS,
                "reasoning_effort": "medium", "profile": "svelte", "judge_backend": "codex",
                "judge_model": MODELS["sol"], "agent_timeout_seconds": 900,
                "attempts_per_task_arm": 3, "planned_runs": len(SCHEDULE),
                "auth": "Codex subscription for agent and judge; TypeSafe API only for routing",
                "prior_sources": {str(path): support.sha(path) for path in PRIOR_PATHS if path.is_file()},
                "source_hashes": _freeze(batch), "runs": [], "status": "preparing",
                "environment_keys": sorted(env),
                "commands": {f"{task}::r{rep}::{arm}": command_for(task, arm)
                             for task, rep, arm in SCHEDULE},
            }
            manifest["git_head"] = support.capture(["git", "rev-parse", "HEAD"], env)["stdout"].strip()
            manifest["git_status"] = support.capture(["git", "status", "--short"], env)["stdout"]
            manifest["applications_head"] = support.capture(
                ["git", "-C", "SREGym-applications", "rev-parse", "HEAD"], env)["stdout"].strip()
            manifest["kubernetes_context"] = support.capture(
                ["kubectl", "config", "current-context"], env)["stdout"].strip()
            support.save_json(batch / "manifest.json", manifest)
        else:
            batch = batch.resolve()
            manifest = json.loads((batch / "manifest.json").read_text())
            if (manifest.get("schedule") != [list(item) for item in SCHEDULE]
                    or manifest.get("status") != "paused_after_limit"
                    or (batch / "completion.json").exists()):
                raise RuntimeError("batch is not a deliberately paused frozen crossover run")
            _validate_frozen(manifest)
        key = getpass.getpass("TypeSafe key for Jev crossover (not saved): ")
        if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
            raise RuntimeError("invalid TypeSafe key")
        start = len(manifest["runs"])
        stop = min(len(SCHEDULE), start + max_runs) if max_runs else len(SCHEDULE)
        manifest["status"] = "running"
        support.save_json(batch / "manifest.json", manifest)
        print(f"Crossover artifacts: {batch}", flush=True)
        for index in range(start, stop):
            summary = _run_one(batch, manifest, env, key, index)
            manifest["runs"].append(summary)
            manifest["status"] = "running" if summary["status"] in {"completed", "completed_model_timeout"} else summary["status"]
            support.save_json(batch / "manifest.json", manifest)
            if manifest["status"] != "running":
                print(f"Stopped after run {index + 1}: {manifest['status']}", flush=True)
                return 1
        if stop < len(SCHEDULE):
            manifest["status"] = "paused_after_limit"
            support.save_json(batch / "manifest.json", manifest)
            print(f"Deliberately paused after {stop}/{len(SCHEDULE)} runs", flush=True)
            return 0
        manifest["status"] = "completed"
        manifest["completed_at"] = _now()
        support.save_json(batch / "manifest.json", manifest)
        support.save_json(batch / "completion.json", {
            "comparison_complete": True, "runs": manifest["runs"], "official_results_rewritten": False,
            "model_timeout_policy": "fixed 900-second timeout counts as failed outcome; no rerun",
        })
        print(f"Crossover complete: {batch}", flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-key", action="store_true", required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--max-runs", type=int)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("--prompt-key requires an interactive terminal")
    if args.max_runs is not None and args.max_runs < 1:
        parser.error("--max-runs must be positive")
    try:
        return run(args.resume, max_runs=args.max_runs)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Crossover not completed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
