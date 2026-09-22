#!/usr/bin/env python3
"""Audit and summarize the completed four-task Jev comparison."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("luna", "terra", "sol", "jev")
TASK_LABELS = {
    "readiness_probe_misconfiguration_social_network": "readiness probe",
    "secret_rotation_stale_env_credentials_astronomy_shop": "secret rotation",
    "cronjob_sidecar_blocks_completion_hotel_reservation": "CronJob sidecar",
    "duplicate_pvc_mounts_social_network": "duplicate PVC",
}


def truth(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _one(paths: list[Path], description: str, *, optional: bool = False) -> Path | None:
    if optional and not paths:
        return None
    if len(paths) != 1:
        raise ValueError(f"expected one {description}, found {len(paths)}")
    return paths[0]


def collect(batch: Path) -> dict:
    batch = batch.resolve()
    completion = json.loads((batch / "completion.json").read_text())
    if not completion.get("comparison_complete") or len(completion.get("runs", [])) != 16:
        raise ValueError("batch is not a completed 16-run comparison")
    manifest = json.loads((batch / "manifest.json").read_text())
    schedule = [tuple(pair) for pair in manifest["schedule"]]
    rows = []
    for offset, run in enumerate(completion["runs"]):
        if ((run.get("task"), run.get("arm")) != schedule[offset]
                or run.get("index") != offset + 1
                or run.get("status") not in {"completed", "completed_model_timeout"}):
            raise ValueError(f"effective run {offset + 1} is not aligned to the frozen schedule")
        result_rows = run.get("result_rows") or []
        if len(result_rows) != 1:
            raise ValueError(f"run {offset + 1} does not have one official result row")
        result = result_rows[0]
        results_root = batch / run["benchmark_results"]
        usage_path = _one(list(results_root.rglob("codex_results_*.json")), "agent usage result", optional=True)
        usage = json.loads(usage_path.read_text())["usage_metrics"] if usage_path else None
        timed_out = truth(result.get("timed_out"))
        if usage is None and not timed_out:
            raise ValueError(f"run {offset + 1} has no agent usage result and is not a timeout")
        route = None
        if run["arm"] == "jev":
            route_path = _one(list(results_root.rglob("routing.json")), "Jev routing result")
            route = json.loads(route_path.read_text())
        elif list(results_root.rglob("routing.json")):
            raise ValueError(f"fixed run {offset + 1} unexpectedly contains a routing result")
        diagnosis_success = truth(result.get("Diagnosis.success"))
        mitigation_success = truth(result.get("Mitigation.success"))
        selected_model = route["selected_model"] if route else run["configured_model"]
        rows.append({
            "index": run["index"], "task": run["task"], "task_label": TASK_LABELS[run["task"]],
            "arm": run["arm"], "selected_model": selected_model, "status": run["status"],
            "diagnosis_accuracy": float(result["Diagnosis.accuracy"]),
            "diagnosis_success": diagnosis_success, "mitigation_success": mitigation_success,
            "joint_success": diagnosis_success and mitigation_success, "timed_out": timed_out,
            "TTL": float(result["TTL"]) if result.get("TTL") else None,
            "TTM": float(result["TTM"]) if result.get("TTM") else None,
            "main_wall_seconds": run.get("main_wall_seconds"),
            "agent_usage": usage, "route": route,
            "artifact": run["benchmark_results"],
        })
    aggregate = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row["arm"] == arm]
        ttms = [row["TTM"] for row in arm_rows if row["TTM"] is not None]
        walls = [row["main_wall_seconds"] for row in arm_rows if row["main_wall_seconds"] is not None]
        usages = [row["agent_usage"] for row in arm_rows if row["agent_usage"]]
        aggregate[arm] = {
            "runs": len(arm_rows),
            "diagnosis_accuracy_mean": sum(row["diagnosis_accuracy"] for row in arm_rows) / len(arm_rows),
            "diagnosis_successes": sum(row["diagnosis_success"] for row in arm_rows),
            "mitigation_successes": sum(row["mitigation_success"] for row in arm_rows),
            "joint_successes": sum(row["joint_success"] for row in arm_rows),
            "model_timeouts": sum(row["timed_out"] for row in arm_rows),
            "TTM_observed_count": len(ttms),
            "TTM_mean_seconds": statistics.mean(ttms) if ttms else None,
            "TTM_median_seconds": statistics.median(ttms) if ttms else None,
            "wall_observed_count": len(walls),
            "wall_mean_seconds": statistics.mean(walls) if walls else None,
            "agent_usage_observed_count": len(usages),
            "agent_total_tokens_known": sum(item["total_tokens"] for item in usages),
            "agent_uncached_input_tokens_known": sum(
                item["input_tokens"] - item.get("cached_input_tokens", 0) for item in usages),
            "agent_output_tokens_known": sum(item["output_tokens"] for item in usages),
        }
    routes = [row for row in rows if row["route"]]
    routing = {
        "runs": len(routes),
        "selection_counts": {
            model: sum(row["selected_model"] == model for row in routes)
            for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")
        },
        "confidence_mean": statistics.mean(row["route"]["confidence"] for row in routes),
        "context_seconds_total": sum(row["route"]["context_collection_seconds"] for row in routes),
        "route_seconds_total": sum(row["route"]["route_latency_seconds"] for row in routes),
        "estimated_cost_usd_total": sum(row["route"]["estimated_cost_usd"] for row in routes),
        "input_tokens_total": sum(row["route"]["usage"]["input_tokens"] for row in routes),
        "output_tokens_total": sum(row["route"]["usage"]["output_tokens"] for row in routes),
    }
    return {
        "batch": str(batch), "protocol": manifest["protocol"], "comparison_complete": True,
        "runs": rows, "aggregate": aggregate, "routing": routing,
        "excluded_infrastructure_attempt_count": len(completion["excluded_infrastructure_attempts"]),
        "official_results_rewritten": completion["official_results_rewritten"],
        "model_timeout_policy": completion["model_timeout_policy"],
        "source_amendments": completion["source_amendments"],
    }


def _f(value, digits=1, missing="—") -> str:
    return missing if value is None else f"{value:.{digits}f}"


def _seconds(value) -> str:
    return "—" if value is None else f"{value:.1f}s"


def markdown(summary: dict) -> str:
    rows = summary["runs"]
    aggregate = summary["aggregate"]
    routing = summary["routing"]
    lines = [
        "# Jev × Codex CLI：4 个 SREGym-Lite 任务的 16 次对照结果",
        "",
        "本批次按 `4 个不同任务 × 4 个策略` 执行：固定 Luna、固定 Terra、固定 Sol、Jev。每个 pair 只运行一次，agent 与 judge 均使用现有 ChatGPT/Codex 订阅；judge 固定为 `gpt-5.6-sol`，reasoning effort 固定为 `medium`。Jev 的 TypeSafe 路由调用单独计费。",
        "",
        "## 结论",
        "",
        "这 4 题没有显示 Jev 带来整体质量提升。Jev 的 diagnosis / mitigation / 两阶段同时成功分别为 2/4、2/4、1/4；固定 Sol 为 3/4、3/4、3/4。Jev 在前三个完成 mitigation 提交的任务上平均 TTM 为 220.2 秒，低于固定 Sol 的 383.7 秒，但第 4 题发生 900 秒 agent timeout，且前两次 Jev→Sol 的质量也低于对应固定 Sol 重复，因此不能把 submission-only 平均值解释为同质量加速。",
        "",
        "Jev 只在 readiness 题选择 Terra，其余三题均选择 Sol，从未选择 Luna。四次 selection confidence 为 0.27–0.33，平均 0.30，表明路由器自己也认为 Sol 与 Terra 的边界接近。路由本身很轻：四次上下文采集加选择共 4.12 秒，TypeSafe 估算总成本 $0.002541。",
        "",
        "## 汇总",
        "",
        "| 策略 | diagnosis 平均分 | diagnosis 通过 | mitigation 通过 | 两阶段同时通过 | TTM 均值* | timeout | 已知 agent tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        a = aggregate[arm]
        lines.append(
            f"| {arm} | {a['diagnosis_accuracy_mean']:.2f} | {a['diagnosis_successes']}/4 | "
            f"{a['mitigation_successes']}/4 | {a['joint_successes']}/4 | "
            f"{_f(a['TTM_mean_seconds'])}s ({a['TTM_observed_count']}/4) | {a['model_timeouts']} | "
            f"{a['agent_total_tokens_known']:,} ({a['agent_usage_observed_count']}/4) |"
        )
    lines += [
        "",
        "\\* TTM 只对已经提交 mitigation 的运行取平均。Jev 的第 4 题在 mitigation 提交前超时，因此该均值有明显幸存者偏差。agent tokens 不含固定 Sol judge，也不等于 API 美元成本；Jev 超时运行没有完整 usage 记录。",
        "",
        "## 逐次结果",
        "",
        "| # | 任务 | 策略 / 实际模型 | diagnosis | mitigation | TTM | wall | agent tokens | Jev confidence |",
        "|---:|---|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        model = row["selected_model"].removeprefix("gpt-5.6-")
        strategy = f"Jev→{model}" if row["arm"] == "jev" else row["arm"]
        usage = row["agent_usage"]
        confidence = row["route"]["confidence"] if row["route"] else None
        mitigation = "通过" if row["mitigation_success"] else ("超时未提交" if row["timed_out"] else "失败")
        lines.append(
            f"| {row['index']} | {row['task_label']} | {strategy} | "
            f"{row['diagnosis_accuracy']:.0f} ({'通过' if row['diagnosis_success'] else '失败'}) | {mitigation} | "
            f"{_seconds(row['TTM'])} | {_seconds(row['main_wall_seconds'])} | "
            f"{usage['total_tokens']:,}" if usage else
            f"| {row['index']} | {row['task_label']} | {strategy} | "
            f"{row['diagnosis_accuracy']:.0f} ({'通过' if row['diagnosis_success'] else '失败'}) | {mitigation} | "
            f"{_seconds(row['TTM'])} | {_seconds(row['main_wall_seconds'])} | —"
        )
        lines[-1] += f" | {_f(confidence, 2)} |"
    lines += [
        "",
        "## Jev 路由明细",
        "",
        "| 任务 | 选择 | confidence | 概率 | 上下文采集 | 路由 | TypeSafe 估算成本 |",
        "|---|---|---:|---|---:|---:|---:|",
    ]
    for row in [item for item in rows if item["route"]]:
        route = row["route"]
        probs = ", ".join(f"{key} {value:.2f}" for key, value in sorted(route["probabilities"].items()))
        lines.append(
            f"| {row['task_label']} | {route['selected_model'].removeprefix('gpt-5.6-')} | "
            f"{route['confidence']:.2f} | {probs} | {route['context_collection_seconds']:.3f}s | "
            f"{route['route_latency_seconds']:.3f}s | ${route['estimated_cost_usd']:.6f} |"
        )
    lines += [
        "",
        "`confidence` 是 Jev 的选择置信度，不是校准后的任务成功概率。路由输入是故障注入后的只读、脱敏 Kubernetes 快照和 OpenAI 官方模型定位；不含 problem ID、oracle、benchmark 源码或既往答案。当前实现只用该快照选模型，没有把快照传给被选中的 Codex agent。duplicate PVC 题展示了这个限制：路由快照把异常 Jaeger Deployment 和 Pending Pod 放在最前面，但被选中的 Sol 仍把注意力转向 OTel DNS，最终诊断 0 分并超时。",
        "",
        "## 审计边界",
        "",
        "- 16 个有效结果严格对应冻结日程中的 16 个 pair；一次缺失嵌套 submodule 的部署失败和一次 Jev 上下文采集 TypeError 均发生在模型调用前，作为 2 个基础设施尝试排除。",
        "- run 7 的模型执行和官方评分已经完成，只因重复的 OCI index 日志行导致归档控制器停止；结果从原产物恢复，没有重跑模型。",
        "- run 14 在固定 900 秒预算内完成了 diagnosis 评分但未提交 mitigation，按模型超时计为两阶段失败，没有重跑。",
        "- run 11 前只修复了 CronJob `status.active` 从整数到引用列表的兼容处理；差异和哈希保存在批次中。官方结果没有改写。",
        "- 这是每个 pair 一次运行的 4 题 pilot，样本太小，且同一 Sol 在固定臂与 Jev 臂结果不同，不能做显著性或稳定性结论。当前只覆盖 Codex CLI，不代表 Claude Code 或 DeepSeek harness。",
        "- Codex agent/judge 使用订阅，没有 OpenAI API 美元账单；TypeSafe 路由四次估算成本合计 "
        f"${routing['estimated_cost_usd_total']:.6f}。",
        "",
        f"原始批次：`{summary['batch']}`",
        "",
    ]
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = ["index", "task", "arm", "selected_model", "status", "diagnosis_accuracy",
              "diagnosis_success", "mitigation_success", "joint_success", "timed_out",
              "TTL", "TTM", "main_wall_seconds", "agent_total_tokens", "routing_confidence",
              "routing_context_seconds", "routing_latency_seconds", "routing_estimated_cost_usd"]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            route = row["route"] or {}
            usage = row["agent_usage"] or {}
            writer.writerow({
                **{key: row.get(key) for key in fields},
                "agent_total_tokens": usage.get("total_tokens"),
                "routing_confidence": route.get("confidence"),
                "routing_context_seconds": route.get("context_collection_seconds"),
                "routing_latency_seconds": route.get("route_latency_seconds"),
                "routing_estimated_cost_usd": route.get("estimated_cost_usd"),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "docs/jev-routing/MULTITASK_RESULTS.zh.md")
    args = parser.parse_args()
    summary = collect(args.batch)
    batch = args.batch.resolve()
    (batch / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    write_csv(batch / "runs.csv", summary["runs"])
    report = args.report if args.report.is_absolute() else ROOT / args.report
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(markdown(summary))
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
