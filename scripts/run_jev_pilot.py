#!/usr/bin/env python3
"""Run the frozen four-arm SREGym subscription pilot sequentially.

Run only when ready to deploy the benchmark: --prompt-key reads Jev's key once
without echo. Importing this module or asking for --help starts no benchmark.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path("/Users/yukun/Documents/SREGym-lite-local/.venv/bin/python")
KUBECONFIG = Path("/Users/yukun/Documents/ChatGPT/research/output/sregym-lite-local-20260916/kubeconfig")
PROBLEM = "service_wrong_pod_selection_hotel_reservation"
SEED = 20260922
MODELS = {name: f"gpt-5.6-{name}" for name in ("luna", "terra", "sol")}
IMAGE_TAG = "sregym-agent-base:latest"
MINUTE_DIRECTORY = re.compile(r"\d{4}_\d{4}\Z")


def frozen_order():
    shuffled = ["luna", "terra", "sol", "jev"]
    random.Random(SEED).shuffle(shuffled)
    return shuffled, ["jev"] + [arm for arm in shuffled if arm != "jev"]


def clean_environment(source):
    """Keep native subscription paths and runtime essentials, never API auth."""
    keep = {
        "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "LANG", "LC_ALL",
        "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE",
        "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
        "API_PORT", "MCP_SERVER_PORT", "K8S_PROXY_PORT", "KUBECTL_VERSION",
    }
    env = {name: value for name, value in source.items() if name in keep}
    env.update(KUBECONFIG=str(KUBECONFIG), PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    return env


def command_for(arm):
    model = MODELS["terra"] if arm == "jev" else MODELS[arm]
    return [
        str(PYTHON), str(ROOT / "main.py"), "--problem", PROBLEM,
        "--agent", "codex", "--model", model,
        "--model-router", "jev" if arm == "jev" else "none",
        "--judge-backend", "codex", "--judge-model", MODELS["sol"],
        "--reasoning-effort", "medium", "--profile", "svelte",
        "--agent-timeout", "900", "--n-attempts", "1",
        "--internet-access", "filtered", "--container-hardening", "on",
        # Each fresh main.py otherwise defaults back to the published image.
        "--force-build",
    ]


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(argv, env, *, required=True):
    """Read-only provenance command; never used to launch agents or deployments."""
    result = subprocess.run(argv, cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
    if required and result.returncode:
        raise RuntimeError(f"Provenance command failed: {argv[0]}")
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def minute_directories():
    results = ROOT / "results"
    return {path for path in results.iterdir() if path.is_dir() and MINUTE_DIRECTORY.fullmatch(path.name)}


def container_ids(env):
    result = capture(["docker", "ps", "--format", "{{.ID}}"], env)
    return set(result["stdout"].splitlines())


def image_identity(env):
    result = capture(["docker", "image", "inspect", IMAGE_TAG, "--format", "{{json .Id}}"], env, required=False)
    return json.loads(result["stdout"]) if result["returncode"] == 0 else None


def freeze_sources(batch):
    """Snapshot runtime source/config, excluding results, credentials and .env."""
    files = {ROOT / name for name in (
        "main.py", "agents.yaml", "pyproject.toml", "uv.lock", ".gitmodules",
        "scripts/run_jev_pilot.py", "docs/jev-routing/PROTOCOL.md",
    )}
    for name in ("clients", "sregym", "llm_backend", "logger", "docker/agents", "docs/jev-routing"):
        for path in (ROOT / name).rglob("*"):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                if path.suffix in {".py", ".sh", ".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".dockerignore"} or path.name.startswith("Dockerfile"):
                    files.add(path)
    hashes = {}
    for source in sorted(files):
        if not source.is_file():
            continue
        relative = source.relative_to(ROOT)
        destination = batch / "configfreeze" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        destination.write_bytes(data)
        hashes[str(relative)] = hashlib.sha256(data).hexdigest()
    return hashes


def classify_result(exit_code, rows):
    """A complete, scored wrong answer is valid; missing/infra evidence stops."""
    if exit_code != 0:
        return "stopped_main_error", ["main_nonzero_exit"]
    if len(rows) != 1:
        return "stopped_incomplete", ["expected_exactly_one_result_row"]
    row = rows[0]
    if row.get("problem_id") != PROBLEM or row.get("attempt") != "1":
        return "stopped_incomplete", ["unexpected_problem_or_attempt"]
    truthy = lambda value: str(value).strip().lower() in {"true", "1", "yes"}
    flags = [name for name in ("deploy_failed", "cleanup_failed", "cleanup_timed_out", "artifact_finalization_failed", "routing_failed") if truthy(row.get(name))]
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


def run_main(argv, env, output, key):
    """Stream combined output to one redacted log and wait for full main exit."""
    started = time.monotonic()
    interrupted = False
    with output.open("w") as log:
        process = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1, start_new_session=True)
        try:
            for line in process.stdout:
                log.write(line.replace(key, "[REDACTED]"))
                log.flush()
            code = process.wait()
        except KeyboardInterrupt:
            interrupted = True
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                if process.poll() is not None:
                    break
                os.killpg(process.pid, sig)
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    continue
            code = process.wait()
        finally:
            process.stdout.close()
    return code, time.monotonic() - started, interrupted


def run_pilot():
    if not PYTHON.is_file() or not KUBECONFIG.is_file():
        raise RuntimeError("The fixed external Python interpreter and kubeconfig must both exist")
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    with (results / ".jev-pilot.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Jev pilot controller is already running") from None
        if minute_directories():
            raise RuntimeError("Existing main.py minute-named results must be preserved elsewhere before this pilot; refusing a possible overwrite")
        env = clean_environment(os.environ)
        provenance = {
            "git_head": capture(["git", "rev-parse", "HEAD"], env)["stdout"].strip(),
            "git_branch": capture(["git", "branch", "--show-current"], env)["stdout"].strip(),
            "git_status": capture(["git", "status", "--short"], env)["stdout"],
            "submodules": capture(["git", "submodule", "status"], env)["stdout"],
            "applications_head": capture(["git", "-C", "SREGym-applications", "rev-parse", "HEAD"], env)["stdout"].strip(),
            "applications_status": capture(["git", "-C", "SREGym-applications", "status", "--short"], env)["stdout"],
            "python": capture([str(PYTHON), "--version"], env)["stdout"].strip(),
            "platform": capture([str(PYTHON), "-c", "import platform; print(platform.platform()); print(platform.machine())"], env)["stdout"],
            "packages": capture([str(PYTHON), "-c", "import importlib.metadata,json; print(json.dumps(sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())))"], env)["stdout"],
            "docker": capture(["docker", "version", "--format", "{{json .Server}}"], env)["stdout"],
            "kubernetes_context": capture(["kubectl", "config", "current-context"], env)["stdout"].strip(),
            "kubernetes_nodes": capture(["kubectl", "get", "nodes", "-o", "json"], env)["stdout"],
        }
        key = getpass.getpass("TypeSafe API key for this pilot (not saved): ")
        if not key or len(key) > 4096 or any(not 33 <= ord(char) <= 126 for char in key):
            raise RuntimeError("Jev key must be nonempty printable text without whitespace")
        batch = results / ("jev-pilot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
        batch.mkdir()
        shuffled, order = frozen_order()
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(), "problem_id": PROBLEM,
            "root": str(ROOT), "python_interpreter": str(PYTHON), "kubeconfig": str(KUBECONFIG),
            "models": MODELS, "seed": SEED, "seed_shuffled_order": shuffled, "execution_order": order,
            "order_policy": "Jev first as integration smoke; remaining fixed arms keep seeded relative order. Not randomized position balance.",
            "judge_backend": "codex", "judge_model": MODELS["sol"], "agent_version": "0.155.1",
            "reasoning_effort": "medium", "profile": "svelte", "agent_timeout_seconds": 900,
            "attempts_per_arm": 1, "stages": "problem default: diagnosis and mitigation",
            "internet_access": "filtered", "container_hardening": "on",
            "force_build_each_arm": True, "agent_image_tag": IMAGE_TAG,
            "jev_preflight_placeholder": MODELS["terra"],
            "auth": "existing Codex subscription for agent and judge", "api_billing_for_codex": False,
            "jev_key_policy": "one getpass; parent memory and Jev main subprocess env only; absent from fixed-arm env, argv and saved controller output",
            "baseline_environment_keys": sorted(env), "jev_environment_keys": sorted([*env, "TYPESAFE_API_KEY"]),
            "provenance": provenance, "source_hashes": freeze_sources(batch),
            "commands": {arm: command_for(arm) for arm in order}, "arms": [], "status": "running",
        }
        save_json(batch / "manifest.json", manifest)
        print(f"Pilot artifacts: {batch}", flush=True)
        for arm in order:
            arm_dir = batch / arm
            arm_dir.mkdir()
            summary = {"arm": arm, "argv": command_for(arm), "configured_model": MODELS.get(arm, MODELS["terra"])}
            try:
                drift = [name for name, expected in manifest["source_hashes"].items() if not (ROOT / name).is_file() or sha(ROOT / name) != expected]
                if drift:
                    raise RuntimeError("Frozen runtime source changed before next arm")
                if minute_directories():
                    raise RuntimeError("A main.py results directory appeared before launch; refusing overwrite")
                containers_before = container_ids(env)
                judge_before = set((ROOT / "logs").glob("judge-codex-*"))
                child_env = dict(env)
                if arm == "jev":
                    child_env["TYPESAFE_API_KEY"] = key
                summary["started_at"] = datetime.now(timezone.utc).isoformat()
                print(f"Starting {arm}; combined output: {arm_dir / 'stdout.log'}", flush=True)
                code, elapsed, interrupted = run_main(command_for(arm), child_env, arm_dir / "stdout.log", key)
                child_env.pop("TYPESAFE_API_KEY", None)
                summary.update(returncode=code, main_wall_seconds=elapsed, interrupted=interrupted,
                               finished_at=datetime.now(timezone.utc).isoformat(), image_id=image_identity(env))
                # main.py is fully exited before moving its minute-resolution
                # root; the next arm can safely reuse even the same minute.
                new_results = sorted(minute_directories())
                if len(new_results) == 1:
                    destination = arm_dir / "benchmark-results"
                    summary["original_results_path"] = str(new_results[0])
                    new_results[0].rename(destination)
                    summary["benchmark_results"] = str(destination.relative_to(batch))
                    csv_path = destination / "codex_ALL_results.csv"
                    summary["results_csv"] = str(csv_path.relative_to(batch))
                    with csv_path.open(newline="") as handle:
                        rows = list(csv.DictReader(handle))
                    summary["result_rows"] = rows
                    summary["status"], summary["stop_reasons"] = classify_result(code, rows)
                else:
                    summary.update(status="stopped_incomplete", stop_reasons=["expected_one_new_main_results_directory"],
                                   discovered_results=[str(path) for path in new_results])
                for path in sorted(set((ROOT / "logs").glob("judge-codex-*")) - judge_before):
                    saved = arm_dir / "judge-logs" / path.name
                    saved.parent.mkdir(exist_ok=True)
                    path.rename(saved)
                leftovers = sorted(container_ids(env) - containers_before)
                summary["unexpected_running_containers"] = leftovers
                if leftovers:
                    summary.update(status="stopped_cleanup_uncertain", stop_reasons=["new_containers_still_running"])
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
                if summary["image_id"] is None:
                    summary.update(status="stopped_infrastructure", stop_reasons=["local_image_identity_unavailable"])
                elif manifest["arms"] and summary["image_id"] != manifest["arms"][0].get("image_id"):
                    summary.update(status="stopped_infrastructure", stop_reasons=["local_image_identity_changed_between_arms"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__,
                               error=str(exc).replace(key, "[REDACTED]"))
            save_json(arm_dir / "result.json", summary)
            manifest["arms"].append(summary)
            manifest["status"] = "running" if summary["status"] == "completed" else summary["status"]
            save_json(batch / "manifest.json", manifest)
            if summary["status"] != "completed":
                print(f"Pilot stopped after {arm}: {summary['status']}. See {arm_dir / 'result.json'}", flush=True)
                return 1
        manifest["status"] = "completed"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        save_json(batch / "manifest.json", manifest)
        print(f"Four-arm pilot completed: {batch}", flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-key", action="store_true", required=True, help="Read Jev key once with getpass; do not use environment or argv credentials")
    parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("--prompt-key requires an interactive terminal for a non-echoing credential prompt")
    try:
        return run_pilot()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Pilot not completed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
