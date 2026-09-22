#!/usr/bin/env python3
"""Summarize saved four-arm SREGym artifacts without running or changing evaluation."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

ARMS = ("jev", "terra", "luna", "sol")
TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "cache_creation_input_tokens", "reasoning_output_tokens")


def read(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def number(value):
    try:
        result = float(value) if not isinstance(value, bool) else float("nan")
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def boolean(value):
    return True if str(value).lower() == "true" else False if str(value).lower() == "false" else None


def show(value):
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "通过" if value else "未通过"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def summarize(batch):
    manifest = read(batch / "manifest.json")
    continuation, completion = read(batch / "continuation.json"), read(batch / "completion.json")
    if not manifest:
        raise ValueError("Missing manifest.json")
    issues, rows = [], []
    resolved = completion.get("arms", [])
    names = [record.get("arm") for record in resolved]
    if len(names) != len(set(names)) or any(name not in ARMS for name in names):
        issues.append("completion arms are duplicated or unexpected")
    resolved_by_arm = {record["arm"]: record for record in resolved}
    expected_order = manifest.get("execution_order", [])
    if len(expected_order) != 4 or set(expected_order) != set(ARMS):
        issues.append("Frozen schedule does not contain four unique expected arms")
    hashes = {"manifest.json": continuation.get("original_manifest_sha256")}
    for arm, digest in continuation.get("original_arm_result_sha256", {}).items():
        hashes[f"{arm}/result.json"] = digest
    for relative, digest in hashes.items():
        if digest and hashlib.sha256((batch / relative).read_bytes()).hexdigest() != digest:
            issues.append(f"Preserved artifact hash differs: {relative}")
    for arm in ARMS:
        saved = read(batch / arm / "result.json")
        resolution = resolved_by_arm.get(arm, {})
        scores = saved.get("result_rows", [])
        score = scores[0] if len(scores) == 1 else {}
        consistent = saved.get("arm") == arm and len(scores) == 1
        if saved.get("results_csv"):
            path = (batch / saved["results_csv"]).resolve()
            consistent = consistent and path.is_relative_to(batch) and path.is_file()
            if consistent:
                with path.open(newline="") as handle:
                    consistent = list(csv.DictReader(handle)) == scores
        else:
            consistent = False
        if resolution and resolution.get("original_result") != saved:
            consistent = False
        cleanup_verified = saved.get("unexpected_running_containers") == []
        cleanup_resolution = resolution.get("cleanup_resolution", {})
        if not cleanup_verified and cleanup_resolution.get("code") == "unrelated_container_verified":
            evidence_path = (batch / cleanup_resolution["evidence_path"]).resolve()
            if evidence_path.is_relative_to(batch) and evidence_path.is_file():
                evidence = read(evidence_path)
                cleanup_verified = (
                    hashlib.sha256(evidence_path.read_bytes()).hexdigest() == cleanup_resolution.get("evidence_sha256")
                    and cleanup_resolution.get("original_flagged_ids") == saved.get("unexpected_running_containers")
                    and evidence.get("flagged_container_running") is False
                    and evidence.get("benchmark_owned_containers_remaining") == []
                    and evidence.get("external_container_created_seconds_after_cleanup", -1) > 0
                    and any(e.get("action") == "destroy" for e in evidence.get("events", []))
                    and all(e.get("name", "").startswith("handoff-") for e in evidence.get("events", []))
                )
        accuracy = number(score.get("Diagnosis.accuracy"))
        diagnosis, mitigation = boolean(score.get("Diagnosis.success")), boolean(score.get("Mitigation.success"))
        official_complete = (consistent and score.get("run_status") == "complete"
                             and score.get("problem_id") == manifest["problem_id"] and str(score.get("attempt")) == "1"
                             and accuracy is not None and 0 <= accuracy <= 100 and diagnosis is not None and mitigation is not None)
        if saved and not consistent:
            issues.append(f"{arm}: result.json, completion record, and official CSV do not agree")
        token_files = list((batch / arm).glob("benchmark-results/codex/*/run_1/codex_results_*.json"))
        raw_usage = read(token_files[0]).get("usage_metrics") if len(token_files) == 1 else None
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        usage = {key: value if isinstance(value := raw_usage.get(key), int) and not isinstance(value, bool) and value >= 0 else None for key in TOKENS}
        route_files = list((batch / arm).glob("benchmark-results/codex/*/routing_attempt1/routing.json"))
        route = read(route_files[0]) if arm == "jev" and len(route_files) == 1 else {}
        model = score.get("routing_model") if arm == "jev" else saved.get("configured_model", manifest["models"].get(arm))
        if arm == "jev" and saved and (not route or route.get("selected_model") != model):
            issues.append("jev: routing artifact and official routing_model do not agree")
        wall, ttm = number(saved.get("main_wall_seconds")), number(score.get("TTM"))
        deploy, cleanup = (number(score.get(f"phase.{name}.duration_s")) for name in ("deploy", "cleanup"))
        other = wall - ttm - deploy - cleanup if all(v is not None for v in (wall, ttm, deploy, cleanup)) else None
        rows.append({"arm": arm, "model": model, "original_status": saved.get("status"),
                     "resolved_status": resolution.get("resolved_status"), "official_complete": official_complete,
                     "returncode": saved.get("returncode"), "interrupted": saved.get("interrupted"),
                     "unexpected_running_containers": saved.get("unexpected_running_containers"),
                     "cleanup_verified": cleanup_verified, "cleanup_resolution": cleanup_resolution,
                     "diagnosis_accuracy": accuracy, "diagnosis_success": diagnosis, "mitigation_success": mitigation,
                     "ttm_seconds": ttm, "main_wall_seconds": wall, "deploy_seconds": deploy, "cleanup_seconds": cleanup,
                     "other_main_seconds": other, "agent_usage": usage,
                     "usage_artifact": str(token_files[0].relative_to(batch)) if len(token_files) == 1 else None,
                     "routing": route if arm == "jev" else {"route_latency_seconds": 0.0, "estimated_cost_usd": 0.0},
                     "phases_seconds": {key: number(value) for key, value in score.items() if key.startswith("phase.") and key.endswith(".duration_s")},
                     "judge_usage": None, "preflight_usage": None, "total_cost_usd": None})
    identities = [(entry.get("build_digests") or {}).get("config") for entry in resolved]
    runtimes = [(entry.get("build_digests") or {}).get("runtime_manifest") for entry in resolved]
    runtime_equal = len(resolved) == 4 and all(identities + runtimes) and len(set(identities)) == len(set(runtimes)) == 1
    if len(resolved) == 4 and not runtime_equal:
        issues.append("Four-arm Docker config/runtime manifest identity is not established")
    complete = (completion.get("comparison_complete") is True and completion.get("status") == "completed"
                and completion.get("original_results_rewritten") is False and set(names) == set(ARMS) and len(names) == 4
                and not issues and runtime_equal and all(r["official_complete"] and r["resolved_status"] == "completed"
                and r["returncode"] == 0 and not r["interrupted"] and r["cleanup_verified"] for r in rows))
    summary = {"comparison_complete": bool(complete), "official_complete_arms": sum(r["official_complete"] for r in rows),
               "planned_arms": 4, "problem_id": manifest["problem_id"], "profile": manifest.get("profile"),
               "original_manifest_status": manifest.get("status"), "continuation_status": continuation.get("status"),
               "completion_status": completion.get("status"), "runtime_identity_equal": bool(runtime_equal),
               "validation_issues": issues, "arms": rows, "judge_usage": None, "preflight_usage": None, "total_cost_usd": None}
    n = summary["official_complete_arms"]
    lines = ["# SREGym：Jev 与三个固定模型", "",
             f"官方完整判分且记录一致：**{n}/4**；四组比较：**{'已完成' if complete else '未完成，不能报告最终完整率'}**。", "",
             f"任务：`{manifest['problem_id']}`；每组仅 1 次。部署 profile：`{manifest.get('profile', 'unknown')}`，不是 full。", "",
             "| 策略 | 执行模型 | 原始→恢复状态 | Diagnosis.accuracy (%) | Diagnosis.success | Mitigation.success | TTM 秒 | main 秒 | deploy 秒 | cleanup 秒 | 其余 main 秒 |",
             "|---|---|---|---:|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        values = [row["arm"], row["model"], f"{row['original_status'] or '未结束/未运行'} → {row['resolved_status'] or '待完成记录'}"]
        values += [row[key] for key in ("diagnosis_accuracy", "diagnosis_success", "mitigation_success", "ttm_seconds", "main_wall_seconds", "deploy_seconds", "cleanup_seconds", "other_main_seconds")]
        lines.append("| " + " | ".join(map(show, values)) + " |")
    lines += ["", "TTM 从故障就绪至修复判分结束，包含路由、CLI 启动、诊断与 judge，失败修复也保留该时长；不能把较短的失败运行当作更快恢复。main 是完整 main.py 墙钟时间。其余 main = main − TTM − deploy − cleanup，包含构建、前置检查、注入等开销及计时差异；细分 phase 原值保存在 summary.json，不能再次叠加到 main。", "",
              "| 策略 | Agent 输入 | 其中缓存读取 | Agent 输出 | 缓存写入 | 其中 reasoning 输出 |",
              "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append("| " + row["arm"] + " | " + " | ".join(show(row["agent_usage"][key]) for key in TOKENS) + " |")
    route = rows[0]["routing"]
    lines += ["", "Agent 用量来自各组 codex_results JSON 的 usage_metrics；缓存读取包含在输入内，reasoning 包含在输出内。judge/preflight 的 token 未由原 bridge 保存，均为 unknown；总美元成本 unknown，不用 API 参考价换算订阅账单。", "",
              f"Jev：confidence={show(route.get('confidence'))}；probabilities=`{json.dumps(route.get('probabilities'), ensure_ascii=False) if route.get('probabilities') is not None else 'unknown'}`；路由延迟={show(route.get('route_latency_seconds'))} 秒；路由 API 估算费用={route.get('estimated_cost_usd') if route.get('estimated_cost_usd') is not None else 'unknown'} 美元。", "",
              "固定 Luna/Terra/Sol 的路由请求开销均为 0。Jev confidence/probabilities 是模型选择输出，不是已校准的任务成功概率；完整路由记录保存在 summary.json。", "",
              "这里只是 1 个真实案例、每组 1 次的 smoke；不能推出总体通过率、模型等价性或普遍路由收益。故障在运行前选定，未根据本轮结果换题；Jev 起始输入只有公开应用信息和官方模型 profile，尚无诊断观测。执行顺序按原 manifest 保留，Jev 首跑，未做到位置平衡。", "",
              "原 phase/oracle 判分保持不变，本汇总不重新判分、不重跑任何组。原 Docker 停止记录保留；恢复仅依据 image config 与非 attestation runtime manifest 一致，index/attestation 变化仍作为 provenance 保留。", "",
              "最终 Sol 组另有一次全局 Docker 扫描误报：无关 handoff 容器在 benchmark 清理结束后才出现，之后已退出并被移除；本次审计没有停止或删除该无关容器。其 lifecycle 证据和清理复核单独保存，原结果标记未改，completion 仅解除该元数据警报。见 [清理核查](postrun-cleanup-audit.json)。", "",
              "四组用户任务与环境文本一致；Codex 原生 developer 提示和 multi_agent_version 会随模型不同，不能声称完整上下文或工具 schema 逐字相同。本轮没有观察到实际子 agent 委派。见 [最终输入审计](final-input-audit.json)。", "",
              "证据：[原 manifest](manifest.json)、[continuation](continuation.json)、[completion](completion.json)、[请求/实际 argv 与环境审计](routing-input-audit.json)、[结构化汇总](summary.json)。"]
    if issues:
        lines += ["", "记录一致性问题："] + [f"- {issue}" for issue in issues]
    return summary, "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path, help="Saved pilot batch directory; no models or evaluator are called")
    args = parser.parse_args()
    try:
        batch = args.batch.resolve()
        summary, report = summarize(batch)
        (batch / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        (batch / "RESULTS.zh.md").write_text(report)
        print(json.dumps({"report": str(batch / "RESULTS.zh.md"), "comparison_complete": summary["comparison_complete"]}))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
