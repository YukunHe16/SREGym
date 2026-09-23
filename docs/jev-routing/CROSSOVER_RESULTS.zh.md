# Jev 任务级模型路由：SREGym-Lite 六题重复对照

本批次在独立 worktree 中完成了 **6 道不同故障 × 3 次独立部署 × 4 种策略 = 72 次** Codex CLI 运行。四种策略是固定 Luna、固定 Terra、固定 Sol，以及 Jev 在 agent 启动前从三者中选一个。三次重复各自重新部署、注入故障和评分；每个策略每题恰好运行三次。全部 agent 和固定 Sol 判官使用 Codex 订阅，Jev 路由使用 TypeSafe API。推理档位均为 `medium`，部署 profile 为 `svelte`，agent 预算为 900 秒。

## 结论

这六题显示了 Jev 的**资源效率信号**，但没有显示质量优于最佳固定模型：Jev 的诊断与修复同时通过 12/18，固定 Sol 13/18，固定 Terra 10/18。相比固定 Sol，Jev 每次运行平均少用约 45 秒完整 wall time（8.0%）和 260k agent token（32.7%），同时少成功一题。因此它有值得重视的 token 节省，但不能称为同质量提速。相比固定 Terra，Jev 多成功两次，代价是每次平均多约 80 秒、159k token。没有预先规定一次失败相当于多少时间或 token，就不能把这些权衡合成单一“净收益”。

Jev 确实按题目改变选择，而且同一题三次都选择同一模型：`wrong_dns_policy`、`internal_traffic_policy` 选 Terra，其余四题选 Sol。它从未选择 Luna。最明显的失误是 `internal_traffic_policy_local_astronomy_shop`：Jev→Terra 0/3，固定 Terra 0/3，固定 Sol 2/3。六题中事后按每题挑表现最好的固定模型，E2E 仅达到 14/18，比始终用 Sol 的 13/18 多一次；这是同批次数据上的乐观上界，说明这批题的可路由质量空间本身也有限。

## 总体结果

| 策略 | 诊断通过 | 修复通过 | E2E 通过 | 平均 TTD | 平均 TTM* | 平均完整 wall | 平均 agent token | 模型超时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 固定 Luna | 5/18 | 10/18 | 5/18 | 85.1 s | 156.5 s | 460.0 s | 494,058 | 0 |
| 固定 Terra | 10/18 | 12/18 | 10/18 | 84.4 s | 140.3 s | 437.1 s | 375,535 | 0 |
| 固定 Sol | 13/18 | 15/18 | 13/18 | 160.8 s | 260.9 s | 562.6 s | 794,132 | 0 |
| Jev | 12/18 | 12/18 | 12/18 | 127.9 s | 217.9 s | 517.5 s | 534,346 | 0 |

\* TTM 是从 agent 开始到修复提交的时间，所有 72 次都有提交；失败运行也在均值内。因此短 TTM 不等于更快完成正确修复。完整 wall 包含部署、判官预检、执行、评分和清理。agent token 不含判官，也不等于 API 美元账单。

把所有尝试（包括失败）消耗的资源除以 E2E 成功次数，Jev 约为 **802k agent token/成功题**，固定 Sol 约 **1,100k token/成功题**，Jev 仍低约 27%；完整 wall time 则约为 776 秒/成功题与 779 秒/成功题，几乎相同。这是这批题上的资源比率，不是 API 美元成本，也不能代替成功率。按同题同重复配对，两组都成功的 9 对中，Jev 平均少 46 秒完整 wall、153k token；这 9 对仅来自三道双方都容易成功的题，不能外推为全部任务的同质量加速。

## 各题 E2E 通过次数

| 故障 | Luna | Terra | Sol | Jev 实际选择 | Jev |
| --- | ---: | ---: | ---: | --- | ---: |
| `wrong_dns_policy_astronomy_shop` | 0/3 | 2/3 | 3/3 | Terra | 3/3 |
| `finalizer_deadlock_controller_hotel_reservation` | 0/3 | 2/3 | 1/3 | Sol | 2/3 |
| `network_policy_block` | 0/3 | 0/3 | 1/3 | Sol | 1/3 |
| `internal_traffic_policy_local_astronomy_shop` | 0/3 | 0/3 | 2/3 | Terra | 0/3 |
| `admission_webhook_outage_hotel_reservation` | 3/3 | 3/3 | 3/3 | Sol | 3/3 |
| `wrong_service_selector_social_network` | 2/3 | 3/3 | 3/3 | Sol | 3/3 |

同题同重复中，Jev 所选模型的固定组只有 10/18 E2E 通过，而 Jev 组为 12/18：同模型两次独立运行也会得到不同成绩。具体有 4 次“Jev 通过、对应固定模型失败”和 2 次反向情况。因此这两次差额不能归因于路由器额外赋予执行模型的能力；路由器只选择模型，不把快照传入 agent 提示词。历史筛题记录里 Luna 看似擅长的几题，本次 Luna 没能稳定复现优势，说明历史单次观察不能当作模型真值。

效率收益最集中在 `wrong_dns_policy_astronomy_shop`：Jev→Terra 和固定 Sol 都是 3/3 E2E 通过，但 Jev 的平均完整 wall 约 465 秒、agent token 321k，固定 Sol 分别约 650 秒、836k。相反，`internal_traffic_policy_local_astronomy_shop` 中 Jev→Terra 0/3，固定 Sol 2/3。当前路由器能够在一部分题上节省资源，但会把另一些题分给成功率较低的模型。

## 路由开销与选择信心

18 次 Jev 选择中，Terra 6 次、Sol 12 次、Luna 0 次。`confidence` 范围 0.25–0.45，均值 0.35；它表示选择信心，未经任务成功率校准。上下文采集合计 7.71 秒，TypeSafe 路由调用合计 8.17 秒，平均合计约 0.88 秒/题。路由请求输入合计 271,654 token、输出 702 token，记录的 TypeSafe 估算费用合计 **$0.011409**。该估算只按记录的输入 token 单价计算，不是服务方账单；Codex agent 与判官使用订阅，也不能从 token 直接推算 API 美元成本。

## 完整性与适用边界

- `completion.json` 标记 72/72 完成；72 份官方结果均为 `completed`，没有模型超时、判官失败或未归档结果。72 份 agent usage、18 份 Jev 输入和18 份 Jev 决策均存在；固定模型组没有路由产物。全部 610 个冻结运行源文件的哈希在结束后仍匹配，运行镜像的 config 与 runtime manifest digest 一致。没有遗留 benchmark 容器。
- 检查了全部 18 份 Jev 请求，未发现 problem ID、oracle、ground truth、fault spec、历史分数或六个真实题目 ID；产物中的 API-key 形态扫描无匹配。首个批次曾在 agent 启动前遭 Cloudflare 1010 拦截，作为[独立的启动失败记录](CROSSOVER_STARTUP.md)保留并排除；兼容性修复后才开始本批 72 次。没有重跑已经完成的模型结果。
- 这六题是根据配置并不一致的历史逐题结果有意挑选的强化对照集，并非随机代表全部 21 道 Lite 题。六题每组只有三次，题目层面的不确定性仍大；不能声称统计显著提升或推广到未见故障。`svelte` 与官方榜单的 `full` profile 不可直接比较。这里仅测 **Codex CLI 的任务开始前一次选择**，没有测 Claude Code、DeepSeek harness 或 Responses API 同一对话中途换模型。

原始与机器可读产物：[逐次 CSV](../../results/jev-crossover-20260923T021419.060149Z/runs.csv)、[按题目和策略汇总 CSV](../../results/jev-crossover-20260923T021419.060149Z/task_arm_summary.csv)、[汇总 JSON](../../results/jev-crossover-20260923T021419.060149Z/summary.json)、[冻结协议](CROSSOVER_PROTOCOL.md)。
