#!/usr/bin/env python3
"""Extract per-run time and token data from a Jev crossover batch."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import statistics

ARMS = ("jev", "luna", "terra", "sol")
FIELDS = (
    "index", "task", "repetition", "arm", "selected_model", "status",
    "diagnosis_accuracy", "diagnosis_success", "mitigation_success", "joint_success", "timed_out",
    "ttd_seconds", "ttm_seconds", "main_wall_seconds",
    "agent_input_tokens", "agent_cached_input_tokens", "agent_output_tokens",
    "agent_reasoning_output_tokens", "agent_total_tokens",
    "router_context_seconds", "router_latency_seconds", "router_input_tokens",
    "router_output_tokens", "router_estimated_cost_usd", "router_confidence",
)


def _truth(value: object) -> bool:
    return str(value).lower() in {"true", "1", "yes"}


def _float(value: object) -> float | None:
    return float(value) if value not in (None, "") else None


def _one(paths: list[Path], label: str, *, optional: bool = False) -> Path | None:
    if optional and not paths:
        return None
    if len(paths) != 1:
        raise ValueError(f"expected one {label}, found {len(paths)}")
    return paths[0]


def collect(batch: Path) -> dict:
    batch = batch.resolve()
    manifest = json.loads((batch / "manifest.json").read_text())
    schedule = manifest["schedule"]
    runs = manifest["runs"]
    if len(runs) > len(schedule):
        raise ValueError("more runs than scheduled")
    rows = []
    for position, run in enumerate(runs):
        if ([run.get("task"), run.get("repetition"), run.get("arm")] != schedule[position]
                or run.get("index") != position + 1
                or run.get("status") not in {"completed", "completed_model_timeout"}):
            raise ValueError(f"run {position + 1} is not an accepted scheduled outcome")
        result_rows = run.get("result_rows") or []
        if len(result_rows) != 1:
            raise ValueError(f"run {position + 1} lacks one official result")
        result = result_rows[0]
        if result.get("problem_id") != run["task"]:
            raise ValueError(f"run {position + 1} problem ID mismatch")
        root = batch / run["benchmark_results"]
        timed_out = run["status"] == "completed_model_timeout" or _truth(result.get("timed_out"))
        usage_path = _one(list(root.rglob("codex_results_*.json")), "agent usage", optional=timed_out)
        usage = json.loads(usage_path.read_text())["usage_metrics"] if usage_path else None
        if usage is None and not timed_out:
            raise ValueError(f"run {position + 1} has no agent usage")
        routes = list(root.rglob("routing.json"))
        route = json.loads(_one(routes, "routing decision").read_text()) if run["arm"] == "jev" else None
        if run["arm"] != "jev" and routes:
            raise ValueError(f"fixed run {position + 1} has a routing decision")
        diagnosis = _truth(result.get("Diagnosis.success"))
        mitigation = _truth(result.get("Mitigation.success"))
        route_usage = route.get("usage") or {} if route else {}
        rows.append({
            "index": run["index"], "task": run["task"], "repetition": run["repetition"],
            "arm": run["arm"], "selected_model": route["selected_model"] if route else run["configured_model"],
            "status": run["status"], "diagnosis_accuracy": _float(result.get("Diagnosis.accuracy")),
            "diagnosis_success": diagnosis, "mitigation_success": mitigation,
            "joint_success": diagnosis and mitigation, "timed_out": timed_out,
            "ttd_seconds": _float(result.get("TTL")), "ttm_seconds": _float(result.get("TTM")),
            "main_wall_seconds": _float(run.get("main_wall_seconds")),
            "agent_input_tokens": usage.get("input_tokens") if usage else None,
            "agent_cached_input_tokens": usage.get("cached_input_tokens") if usage else None,
            "agent_output_tokens": usage.get("output_tokens") if usage else None,
            "agent_reasoning_output_tokens": usage.get("reasoning_output_tokens") if usage else None,
            "agent_total_tokens": usage.get("total_tokens") if usage else None,
            "router_context_seconds": route.get("context_collection_seconds") if route else None,
            "router_latency_seconds": route.get("route_latency_seconds") if route else None,
            "router_input_tokens": route_usage.get("input_tokens"),
            "router_output_tokens": route_usage.get("output_tokens"),
            "router_estimated_cost_usd": route.get("estimated_cost_usd") if route else None,
            "router_confidence": route.get("confidence") if route else None,
        })
    aggregate = {}
    for arm in ARMS:
        group = [row for row in rows if row["arm"] == arm]
        totals = [row["agent_total_tokens"] for row in group if row["agent_total_tokens"] is not None]
        ttms = [row["ttm_seconds"] for row in group if row["ttm_seconds"] is not None]
        walls = [row["main_wall_seconds"] for row in group if row["main_wall_seconds"] is not None]
        aggregate[arm] = {
            "runs": len(group), "joint_successes": sum(row["joint_success"] for row in group),
            "timeouts": sum(row["timed_out"] for row in group),
            "agent_tokens_known_sum": sum(totals), "agent_tokens_observed_count": len(totals),
            "ttm_observed_mean_seconds": statistics.mean(ttms) if ttms else None,
            "ttm_observed_count": len(ttms),
            "wall_mean_seconds": statistics.mean(walls) if walls else None,
            "wall_observed_count": len(walls),
        }
    by_task_arm = []
    for task in dict.fromkeys(item[0] for item in schedule):
        for arm in ARMS:
            group = [row for row in rows if row["task"] == task and row["arm"] == arm]
            if not group:
                continue
            ttms = [row["ttm_seconds"] for row in group if row["ttm_seconds"] is not None]
            walls = [row["main_wall_seconds"] for row in group if row["main_wall_seconds"] is not None]
            tokens = [row["agent_total_tokens"] for row in group if row["agent_total_tokens"] is not None]
            by_task_arm.append({
                "task": task, "arm": arm, "runs": len(group),
                "joint_successes": sum(row["joint_success"] for row in group),
                "timeouts": sum(row["timed_out"] for row in group),
                "ttm_observed_mean_seconds": statistics.mean(ttms) if ttms else None,
                "ttm_observed_count": len(ttms),
                "wall_mean_seconds": statistics.mean(walls) if walls else None,
                "wall_observed_count": len(walls),
                "agent_tokens_known_sum": sum(tokens),
                "agent_tokens_observed_count": len(tokens),
                "agent_tokens_observed_mean": statistics.mean(tokens) if tokens else None,
            })
    return {"batch": str(batch), "status": manifest["status"], "accepted_runs": len(rows),
            "planned_runs": len(schedule), "comparison_complete": (batch / "completion.json").exists(),
            "runs": rows, "aggregate": aggregate, "by_task_arm": by_task_arm}


def write(batch: Path, summary: dict) -> None:
    with (batch / "runs.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(summary["runs"])
    if summary["by_task_arm"]:
        with (batch / "task_arm_summary.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=summary["by_task_arm"][0].keys())
            writer.writeheader()
            writer.writerows(summary["by_task_arm"])
    (batch / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    summary = collect(args.batch)
    write(args.batch.resolve(), summary)
    print(f"{summary['accepted_runs']}/{summary['planned_runs']} accepted runs; {args.batch.resolve() / 'runs.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
