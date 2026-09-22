#!/usr/bin/env python3
"""Run the frozen four-task, four-arm SREGym Jev pilot sequentially."""

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
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__:
    from . import run_jev_pilot as support
else:
    spec = importlib.util.spec_from_file_location("jev_pilot_support", Path(__file__).with_name("run_jev_pilot.py"))
    support = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(support)

PYTHON = support.PYTHON
KUBECONFIG = support.KUBECONFIG
MODELS = support.MODELS
PROTOCOL = ROOT / "docs/jev-routing/MULTITASK_PROTOCOL.md"
TASK_ORDERS = (
    ("readiness_probe_misconfiguration_social_network", ("jev", "luna", "terra", "sol")),
    ("secret_rotation_stale_env_credentials_astronomy_shop", ("luna", "terra", "sol", "jev")),
    ("cronjob_sidecar_blocks_completion_hotel_reservation", ("terra", "sol", "jev", "luna")),
    ("duplicate_pvc_mounts_social_network", ("sol", "jev", "luna", "terra")),
)
SCHEDULE = tuple((task, arm) for task, arms in TASK_ORDERS for arm in arms)


def command_for(task: str, arm: str) -> list[str]:
    model = MODELS["terra"] if arm == "jev" else MODELS[arm]
    return [
        str(PYTHON), str(ROOT / "main.py"), "--problem", task,
        "--agent", "codex", "--model", model,
        "--model-router", "jev" if arm == "jev" else "none",
        "--judge-backend", "codex", "--judge-model", MODELS["sol"],
        "--reasoning-effort", "medium", "--profile", "svelte",
        "--agent-timeout", "900", "--n-attempts", "1",
        "--internet-access", "filtered", "--container-hardening", "on", "--force-build",
    ]


def classify(task: str, returncode: int, rows: list[dict]) -> tuple[str, list[str]]:
    if returncode != 0:
        return "stopped_main_error", ["main_nonzero_exit"]
    if len(rows) != 1 or rows[0].get("problem_id") != task or rows[0].get("attempt") != "1":
        return "stopped_incomplete", ["unexpected_result_coverage"]
    row = rows[0]
    yes = lambda value: str(value).strip().lower() in {"true", "1", "yes"}
    flags = [name for name in ("deploy_failed", "cleanup_failed", "cleanup_timed_out",
                                "artifact_finalization_failed", "routing_failed") if yes(row.get(name))]
    if flags:
        return "stopped_infrastructure", flags
    if row.get("run_status") != "complete":
        return "stopped_incomplete", [row.get("incomplete_reason") or "run_not_complete"]
    for stage in ("Diagnosis", "Mitigation"):
        if str(row.get(stage + ".success", "")).strip().lower() not in {"true", "false", "1", "0"}:
            return "stopped_incomplete", ["missing_" + stage.lower() + "_verdict"]
        if row.get(stage + ".error") or row.get(stage + ".failure_class") in {"environment_error", "harness_error"}:
            return "stopped_evaluator_error", [stage.lower() + "_evaluation_error"]
    return "completed", []


def freeze_sources(batch: Path) -> dict[str, str]:
    files = []
    for name in ("main.py", "agents.yaml", "pyproject.toml", "uv.lock", ".gitmodules",
                 "scripts/run_jev_multitask.py", "docs/jev-routing/MULTITASK_PROTOCOL.md"):
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for name in ("clients", "sregym", "llm_backend", "logger", "docker/agents", "docs/jev-routing"):
        for path in (ROOT / name).rglob("*"):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                if path.suffix in {".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".dockerignore"} or path.name.startswith("Dockerfile"):
                    files.append(path)
    hashes = {}
    for source in sorted(set(files)):
        relative = source.relative_to(ROOT)
        destination = batch / "configfreeze" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        hashes[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
    return hashes


def _save(path: Path, value: dict) -> None:
    support.save_json(path, value)


def run() -> int:
    if not PYTHON.is_file() or not KUBECONFIG.is_file():
        raise RuntimeError("fixed Python interpreter and kubeconfig are required")
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    with (results / ".jev-multitask.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another multitask pilot is running") from None
        if support.minute_directories():
            raise RuntimeError("unarchived main.py result directories already exist")
        env = support.clean_environment(os.environ)
        key = getpass.getpass("TypeSafe API key for multitask pilot (not saved): ")
        if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
            raise RuntimeError("invalid TypeSafe key")
        batch = results / ("jev-multitask-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
        batch.mkdir()
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(), "protocol": str(PROTOCOL.relative_to(ROOT)),
            "schedule": [list(value) for value in SCHEDULE], "tasks": [task for task, _ in TASK_ORDERS],
            "models": MODELS, "reasoning_effort": "medium", "profile": "svelte",
            "judge_backend": "codex", "judge_model": MODELS["sol"], "agent_version": "0.155.1",
            "attempts_per_pair": 1, "agent_timeout_seconds": 900, "planned_runs": len(SCHEDULE),
            "auth": "ChatGPT subscription for agent and judge", "codex_api_billing": False,
            "router_context": "post-injection bounded read-only snapshot through filtered agent kubeconfig",
            "key_policy": "getpass once; only Jev host main processes; never fixed arms or containers",
            "environment_keys": sorted(env), "commands": {f"{task}::{arm}": command_for(task, arm) for task, arm in SCHEDULE},
            "source_hashes": {}, "runs": [], "status": "preparing",
        }
        manifest["source_hashes"] = freeze_sources(batch)
        manifest["git_head"] = support.capture(["git", "rev-parse", "HEAD"], env)["stdout"].strip()
        manifest["git_branch"] = support.capture(["git", "branch", "--show-current"], env)["stdout"].strip()
        manifest["git_status"] = support.capture(["git", "status", "--short"], env)["stdout"]
        manifest["applications_head"] = support.capture(
            ["git", "-C", "SREGym-applications", "rev-parse", "HEAD"], env)["stdout"].strip()
        manifest["kubernetes_context"] = support.capture(["kubectl", "config", "current-context"], env)["stdout"].strip()
        manifest["status"] = "running"
        _save(batch / "manifest.json", manifest)
        print(f"Multitask pilot artifacts: {batch}", flush=True)
        reference_runtime = None
        for index, (task, arm) in enumerate(SCHEDULE, start=1):
            run_dir = batch / f"{index:02d}-{task}--{arm}"
            run_dir.mkdir()
            summary = {"index": index, "task": task, "arm": arm, "argv": command_for(task, arm),
                       "configured_model": MODELS.get(arm, MODELS["terra"])}
            try:
                drift = [name for name, digest in manifest["source_hashes"].items()
                         if not (ROOT / name).is_file() or support.sha(ROOT / name) != digest]
                if drift:
                    raise RuntimeError("frozen source changed before run: " + ", ".join(drift[:3]))
                if support.minute_directories():
                    raise RuntimeError("unarchived main.py result directory appeared")
                before = support.container_ids(env)
                judge_before = set((ROOT / "logs").glob("judge-codex-*"))
                child_env = dict(env)
                if arm == "jev":
                    child_env["TYPESAFE_API_KEY"] = key
                summary["started_at"] = datetime.now(timezone.utc).isoformat()
                print(f"[{index}/{len(SCHEDULE)}] {task} / {arm}", flush=True)
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
                summary["status"], summary["stop_reasons"] = classify(task, code, summary["result_rows"])
                for path in sorted(set((ROOT / "logs").glob("judge-codex-*")) - judge_before):
                    saved = run_dir / "judge-logs" / path.name
                    saved.parent.mkdir(exist_ok=True)
                    path.rename(saved)
                leftovers = sorted(support.container_ids(env) - before)
                summary["unexpected_running_benchmark_containers"] = leftovers
                if leftovers:
                    summary.update(status="stopped_cleanup_uncertain", stop_reasons=["benchmark_containers_still_running"])
                runtime = support.runtime_identity(summary["build_digests"])
                reference_runtime = reference_runtime or runtime
                if runtime != reference_runtime or summary["image_id"] != summary["build_digests"]["index"]:
                    summary.update(status="stopped_infrastructure", stop_reasons=["runtime_image_identity_changed"])
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__,
                               error=str(exc).replace(key, "[REDACTED]"))
            _save(run_dir / "result.json", summary)
            manifest["runs"].append(summary)
            manifest["status"] = "running" if summary["status"] == "completed" else summary["status"]
            _save(batch / "manifest.json", manifest)
            if summary["status"] != "completed":
                print(f"Stopped after run {index}: {summary['status']}", flush=True)
                return 1
        manifest["status"] = "completed"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save(batch / "manifest.json", manifest)
        _save(batch / "completion.json", {"comparison_complete": True, "runs": manifest["runs"],
                                           "official_results_rewritten": False})
        print(f"Multitask pilot complete: {batch}", flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-key", action="store_true", required=True)
    parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("--prompt-key requires an interactive terminal")
    try:
        return run()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Multitask pilot not completed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
