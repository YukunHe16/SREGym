# Jev × Codex CLI：4 个 SREGym-Lite 任务的 16 次对照结果

本批次按 `4 个不同任务 × 4 个策略` 执行：固定 Luna、固定 Terra、固定 Sol、Jev。每个 pair 只运行一次，agent 与 judge 均使用现有 ChatGPT/Codex 订阅；judge 固定为 `gpt-5.6-sol`，reasoning effort 固定为 `medium`。Jev 的 TypeSafe 路由调用单独计费。

## 结论

这 4 题没有显示 Jev 带来整体质量提升。Jev 的 diagnosis / mitigation / 两阶段同时成功分别为 2/4、2/4、1/4；固定 Sol 为 3/4、3/4、3/4。Jev 在前三个完成 mitigation 提交的任务上平均 TTM 为 220.2 秒，低于固定 Sol 的 383.7 秒，但第 4 题发生 900 秒 agent timeout，且前两次 Jev→Sol 的质量也低于对应固定 Sol 重复，因此不能把 submission-only 平均值解释为同质量加速。

Jev 只在 readiness 题选择 Terra，其余三题均选择 Sol，从未选择 Luna。四次 selection confidence 为 0.27–0.33，平均 0.30，表明路由器自己也认为 Sol 与 Terra 的边界接近。路由本身很轻：四次上下文采集加选择共 4.12 秒，TypeSafe 估算总成本 $0.002541。

## 汇总

| 策略 | diagnosis 平均分 | diagnosis 通过 | mitigation 通过 | 两阶段同时通过 | TTM 均值* | timeout | 已知 agent tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| luna | 66.75 | 2/4 | 3/4 | 1/4 | 219.8s (4/4) | 0 | 1,699,556 (4/4) |
| terra | 72.25 | 3/4 | 1/4 | 1/4 | 221.2s (4/4) | 0 | 2,740,245 (4/4) |
| sol | 75.00 | 3/4 | 3/4 | 3/4 | 383.7s (4/4) | 0 | 3,608,704 (4/4) |
| jev | 58.50 | 2/4 | 2/4 | 1/4 | 220.2s (3/4) | 1 | 1,021,395 (3/4) |

\* TTM 只对已经提交 mitigation 的运行取平均。Jev 的第 4 题在 mitigation 提交前超时，因此该均值有明显幸存者偏差。agent tokens 不含固定 Sol judge，也不等于 API 美元成本；Jev 超时运行没有完整 usage 记录。

## 逐次结果

| # | 任务 | 策略 / 实际模型 | diagnosis | mitigation | TTM | wall | agent tokens | Jev confidence |
|---:|---|---|---:|---|---:|---:|---:|---:|
| 1 | readiness probe | Jev→terra | 100 (通过) | 通过 | 120.5s | 325.4s | 185,565 | 0.27 |
| 2 | readiness probe | luna | 100 (通过) | 通过 | 126.2s | 407.0s | 169,557 | — |
| 3 | readiness probe | terra | 100 (通过) | 通过 | 121.8s | 433.4s | 189,157 | — |
| 4 | readiness probe | sol | 100 (通过) | 通过 | 209.8s | 518.9s | 364,553 | — |
| 5 | secret rotation | luna | 0 (失败) | 通过 | 294.1s | 609.5s | 545,878 | — |
| 6 | secret rotation | terra | 89 (通过) | 失败 | 264.9s | 576.1s | 771,361 | — |
| 7 | secret rotation | sol | 100 (通过) | 通过 | 458.4s | — | 1,380,073 | — |
| 8 | secret rotation | Jev→sol | 34 (失败) | 通过 | 332.3s | 647.7s | 629,030 | 0.33 |
| 9 | CronJob sidecar | terra | 100 (通过) | 失败 | 95.5s | 490.8s | 174,554 | — |
| 10 | CronJob sidecar | sol | 100 (通过) | 通过 | 320.1s | 701.1s | 473,183 | — |
| 11 | CronJob sidecar | Jev→sol | 100 (通过) | 失败 | 207.7s | 571.8s | 206,800 | 0.28 |
| 12 | CronJob sidecar | luna | 100 (通过) | 失败 | 253.7s | 620.0s | 400,454 | — |
| 13 | duplicate PVC | sol | 0 (失败) | 失败 | 546.5s | 844.0s | 1,390,895 | — |
| 14 | duplicate PVC | Jev→sol | 0 (失败) | 超时未提交 | — | 1211.5s | — | 0.32 |
| 15 | duplicate PVC | luna | 67 (失败) | 通过 | 205.0s | 475.1s | 583,667 | — |
| 16 | duplicate PVC | terra | 0 (失败) | 失败 | 402.7s | 711.3s | 1,605,173 | — |

## Jev 路由明细

| 任务 | 选择 | confidence | 概率 | 上下文采集 | 路由 | TypeSafe 估算成本 |
|---|---|---:|---|---:|---:|---:|
| readiness probe | terra | 0.27 | luna 0.00, sol 0.49, terra 0.51 | 0.443s | 0.540s | $0.000629 |
| secret rotation | sol | 0.33 | luna 0.00, sol 0.56, terra 0.44 | 0.437s | 0.500s | $0.000646 |
| CronJob sidecar | sol | 0.28 | luna 0.00, sol 0.52, terra 0.48 | 0.395s | 0.690s | $0.000623 |
| duplicate PVC | sol | 0.32 | luna 0.00, sol 0.55, terra 0.45 | 0.420s | 0.693s | $0.000642 |

`confidence` 是 Jev 的选择置信度，不是校准后的任务成功概率。路由输入是故障注入后的只读、脱敏 Kubernetes 快照和 OpenAI 官方模型定位；不含 problem ID、oracle、benchmark 源码或既往答案。当前实现只用该快照选模型，没有把快照传给被选中的 Codex agent。duplicate PVC 题展示了这个限制：路由快照把异常 Jaeger Deployment 和 Pending Pod 放在最前面，但被选中的 Sol 仍把注意力转向 OTel DNS，最终诊断 0 分并超时。

## 审计边界

- 16 个有效结果严格对应冻结日程中的 16 个 pair；一次缺失嵌套 submodule 的部署失败和一次 Jev 上下文采集 TypeError 均发生在模型调用前，作为 2 个基础设施尝试排除。
- run 7 的模型执行和官方评分已经完成，只因重复的 OCI index 日志行导致归档控制器停止；结果从原产物恢复，没有重跑模型。
- run 14 在固定 900 秒预算内完成了 diagnosis 评分但未提交 mitigation，按模型超时计为两阶段失败，没有重跑。
- run 11 前只修复了 CronJob `status.active` 从整数到引用列表的兼容处理；差异和哈希保存在批次中。官方结果没有改写。
- 这是每个 pair 一次运行的 4 题 pilot，样本太小，且同一 Sol 在固定臂与 Jev 臂结果不同，不能做显著性或稳定性结论。当前只覆盖 Codex CLI，不代表 Claude Code 或 DeepSeek harness。
- Codex agent/judge 使用订阅，没有 OpenAI API 美元账单；TypeSafe 路由四次估算成本合计 $0.002541。

原始批次：`/Users/yukun/Documents/SREGym-jev-routing/results/jev-multitask-20260922T042059.591192Z`
