#!/usr/bin/env python3
"""Resume only the image-index metadata stop in the frozen Jev pilot.

The original manifest and completed arm results remain unchanged. This script
never reads a router key or runs Jev; only the frozen remaining Luna/Sol arms run.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess

if __package__:
    from . import run_jev_pilot as pilot
else:
    import run_jev_pilot as pilot

STOP_REASON = "local_image_identity_changed_between_arms"
CORRECTION = (
    "Compare the Docker build's image config digest and non-attestation runtime manifest digest. "
    "An OCI index/image.Id can change solely because an attestation changes. Keep index IDs as "
    "provenance; do not treat that metadata change alone as changed runtime bytes."
)


def build_digests(log_path):
    text = Path(log_path).read_text(errors="replace")
    found = {}
    for name, label in (("config", "config"), ("runtime_manifest", "manifest"), ("index", "manifest list")):
        values = re.findall(r"exporting " + label + r" (sha256:[0-9a-f]{64})(?=\s|$)", text)
        if len(values) != 1:
            raise ValueError(f"Expected exactly one {name} digest in the complete build log")
        found[name] = values[0]
    found["attestations"] = re.findall(r"exporting attestation manifest (sha256:[0-9a-f]{64})(?=\s|$)", text)
    return found


def runtime_identity(digests):
    return digests["config"], digests["runtime_manifest"]


def check_sources(manifest, root):
    for name, expected in manifest["source_hashes"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or pilot.sha(path) != expected:
            raise ValueError("Original frozen source hash no longer matches: " + name)


def validate_resume(batch, root=pilot.ROOT):
    batch, root = Path(batch).resolve(), Path(root).resolve()
    manifest = json.loads((batch / "manifest.json").read_text())
    if manifest.get("status") != "stopped_infrastructure":
        raise ValueError("Original pilot has not stopped for the permitted infrastructure reason")
    if manifest.get("execution_order") != ["jev", "terra", "luna", "sol"]:
        raise ValueError("Only the original frozen Jev/Terra/Luna/Sol schedule is supported")
    arms = manifest.get("arms", [])
    if [row.get("arm") for row in arms] != ["jev", "terra"]:
        raise ValueError("Exactly the original Jev and Terra runs must already be recorded")
    if arms[0].get("status") != "completed" or arms[1].get("status") != "stopped_infrastructure" or arms[1].get("stop_reasons") != [STOP_REASON]:
        raise ValueError("The last arm did not stop exclusively because of image index identity")
    check_sources(manifest, root)
    evidence = {}
    for row in arms:
        arm = row["arm"]
        original = json.loads((batch / arm / "result.json").read_text())
        if original != row:
            raise ValueError("Original per-arm result and manifest disagree")
        if row.get("returncode") != 0 or row.get("interrupted") is not False or row.get("unexpected_running_containers") != []:
            raise ValueError("Original main exit or cleanup was not successful")
        if pilot.classify_result(row["returncode"], row.get("result_rows", [])) != ("completed", []):
            raise ValueError("Original benchmark results are not complete and valid")
        csv_path = (batch / row["results_csv"]).resolve()
        if not csv_path.is_relative_to(batch):
            raise ValueError("Original CSV path is outside the pilot")
        with csv_path.open(newline="") as handle:
            if list(csv.DictReader(handle)) != row["result_rows"]:
                raise ValueError("Original CSV does not match preserved benchmark results")
        digests = build_digests(batch / arm / "stdout.log")
        if digests["index"] != row.get("image_id"):
            raise ValueError("Recorded image index does not match its build log")
        evidence[arm] = digests
    if runtime_identity(evidence["jev"]) != runtime_identity(evidence["terra"]):
        raise ValueError("Existing runs used different runtime image bytes")
    for arm in ("luna", "sol"):
        if (batch / arm).exists():
            raise ValueError("A remaining arm already has artifacts; automatic replay is forbidden")
        if manifest["commands"][arm] != pilot.command_for(arm):
            raise ValueError("Remaining command differs from the original frozen protocol")
    if (batch / "continuation.json").exists() or (batch / "completion.json").exists():
        raise ValueError("This pilot already has a continuation; retries are forbidden")
    return manifest, evidence


def _completion(manifest, evidence, rows):
    resolved = []
    for original in manifest["arms"]:
        resolved.append({
            "arm": original["arm"], "original_result": original,
            "resolved_status": "completed",
            "resolved_metadata_flags": [STOP_REASON] if original["arm"] == "terra" else [],
            "resolution": "Equal config and runtime manifest; only index/attestation metadata differed" if original["arm"] == "terra" else None,
            "build_digests": evidence[original["arm"]],
        })
    resolved.extend({"arm": row["arm"], "original_result": row, "resolved_status": row["status"], "resolved_metadata_flags": [], "build_digests": row.get("build_digests")} for row in rows)
    complete = len(resolved) == 4 and all(row["resolved_status"] == "completed" for row in resolved)
    return {"comparison_complete": complete, "correction": CORRECTION, "original_results_rewritten": False,
            "execution_order": manifest["execution_order"], "arms": resolved,
            "status": "completed" if complete else (rows[-1]["status"] if rows else "running")}


def continue_pilot(batch):
    batch = Path(batch).resolve()
    root = pilot.ROOT
    with (root / "results/.jev-pilot.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("The original controller or another continuation is still running") from None
        manifest, evidence = validate_resume(batch)
        if pilot.minute_directories():
            raise ValueError("Unarchived main results exist; cleanup is not established")
        env = pilot.clean_environment(os.environ)
        source = Path(__file__).resolve()
        snapshot = batch / "continuation_source" / source.name
        snapshot.parent.mkdir()
        snapshot.write_bytes(source.read_bytes())
        continuation = {
            "created_at": datetime.now(timezone.utc).isoformat(), "correction": CORRECTION,
            "original_manifest_sha256": pilot.sha(batch / "manifest.json"),
            "original_arm_result_sha256": {arm: pilot.sha(batch / arm / "result.json") for arm in ("jev", "terra")},
            "original_build_log_sha256": {arm: pilot.sha(batch / arm / "stdout.log") for arm in ("jev", "terra")},
            "existing_build_digests": evidence, "remaining_arms": ["luna", "sol"],
            "source_snapshot": str(snapshot.relative_to(batch)), "source_sha256": pilot.sha(snapshot),
            "environment_keys": sorted(env), "router_calls": 0, "credential_prompt": False,
            "original_metadata_preserved": True, "status": "running", "new_arms": [],
        }
        pilot.save_json(batch / "continuation.json", continuation)
        reference = runtime_identity(evidence["jev"])
        for arm in ("luna", "sol"):
            arm_dir = batch / arm
            arm_dir.mkdir()
            summary = {"arm": arm, "argv": manifest["commands"][arm], "configured_model": pilot.MODELS[arm]}
            try:
                check_sources(manifest, root)
                if pilot.minute_directories():
                    raise ValueError("A new main results directory exists before launch")
                before = pilot.container_ids(env)
                judge_before = set((root / "logs").glob("judge-codex-*"))
                summary["started_at"] = datetime.now(timezone.utc).isoformat()
                print(f"Continuing frozen {arm}; log: {arm_dir / 'stdout.log'}", flush=True)
                # Legacy logging accepts a redaction string. A NUL-delimited
                # noncredential sentinel avoids empty-string replacement.
                code, elapsed, interrupted = pilot.run_main(summary["argv"], env, arm_dir / "stdout.log", "\x00NO_ROUTER_CREDENTIAL\x00")
                summary.update(returncode=code, main_wall_seconds=elapsed, interrupted=interrupted,
                               finished_at=datetime.now(timezone.utc).isoformat(), image_id=pilot.image_identity(env))
                created = sorted(pilot.minute_directories())
                if len(created) != 1:
                    raise ValueError("Expected exactly one new main results directory")
                destination = arm_dir / "benchmark-results"
                summary["original_results_path"] = str(created[0])
                created[0].rename(destination)
                csv_path = destination / "codex_ALL_results.csv"
                summary.update(benchmark_results=str(destination.relative_to(batch)), results_csv=str(csv_path.relative_to(batch)))
                with csv_path.open(newline="") as handle:
                    summary["result_rows"] = list(csv.DictReader(handle))
                summary["status"], summary["stop_reasons"] = pilot.classify_result(code, summary["result_rows"])
                for path in sorted(set((root / "logs").glob("judge-codex-*")) - judge_before):
                    saved = arm_dir / "judge-logs" / path.name
                    saved.parent.mkdir(exist_ok=True)
                    path.rename(saved)
                leftovers = sorted(pilot.container_ids(env) - before)
                summary["unexpected_running_containers"] = leftovers
                if leftovers:
                    summary.update(status="stopped_cleanup_uncertain", stop_reasons=["new_containers_still_running"])
                if interrupted:
                    summary.update(status="stopped_interrupted", stop_reasons=["user_interrupt"])
                summary["build_digests"] = build_digests(arm_dir / "stdout.log")
                if runtime_identity(summary["build_digests"]) != reference:
                    summary.update(status="stopped_infrastructure", stop_reasons=["runtime_image_config_or_manifest_changed"])
                if summary["image_id"] != summary["build_digests"]["index"]:
                    summary.update(status="stopped_infrastructure", stop_reasons=["recorded_index_disagrees_with_build_log"])
            except Exception as exc:
                summary.update(status="stopped_controller_error", error_type=type(exc).__name__, error=str(exc))
            pilot.save_json(arm_dir / "result.json", summary)
            continuation["new_arms"].append(summary)
            continuation["status"] = "running" if summary["status"] == "completed" else summary["status"]
            pilot.save_json(batch / "continuation.json", continuation)
            pilot.save_json(batch / "completion.json", _completion(manifest, evidence, continuation["new_arms"]))
            if summary["status"] != "completed":
                print(f"Continuation stopped: {summary['status']}", flush=True)
                return 1
        continuation["status"] = "completed"
        continuation["completed_at"] = datetime.now(timezone.utc).isoformat()
        pilot.save_json(batch / "continuation.json", continuation)
        print(f"Four-arm evidence complete: {batch / 'completion.json'}", flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true", help="Validate frozen evidence without running models or writing artifacts")
    args = parser.parse_args()
    try:
        if args.check_only:
            _, evidence = validate_resume(args.baseline)
            print(json.dumps({"eligible": True, "remaining_arms": ["luna", "sol"], "build_digests": evidence}, indent=2))
            return 0
        return continue_pilot(args.baseline)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Continuation refused: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
