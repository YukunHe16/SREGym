# Jev × Codex：SREGym 三模型路由试跑

已在独立 worktree `/Users/yukun/Documents/SREGym-jev-routing`、分支 `codex/jev-model-routing` 中完成接入，并真实跑完同一 SREGym 故障的四组对照。原工作树没有加入这些代码。

Jev 每个任务开始前选择一次 Luna、Terra 或 Sol。请求包含公开应用信息和 [OpenAI 官方模型说明](https://learn.chatgpt.com/docs/models)的带来源摘要；不包含故障 ID、答案、评测规则或历史得分。三模型的来源快照、核验时间与哈希见 [model-sources.md](model-sources.md) 和 [JSON profile](../../sregym/routing/openai_models_20260922.json)。此前手写的“优先 Luna”指令已被移除。

本次任务是酒店服务的错误后端选择：前端 Service 将不提供对应 HTTP 端口的 search pod 也纳入后端，造成间歇性请求失败。四组分别重新部署并注入故障，使用 Codex CLI **0.155.1**、`medium`、`svelte`、900 秒 agent 上限。诊断 judge 固定为 Sol，执行模型和 judge 都走现有 ChatGPT 订阅；该 checkout 的诊断与修复评估逻辑未改动。

| 策略 | 实际模型 | 诊断分 | 诊断通过 | 修复通过 | 处理耗时 |
|---|---|---:|---|---|---:|
| Jev | Sol | 100% | 是 | 是 | 209.08 秒 |
| 固定 Luna | Luna | 67% | 否 | 是 | 128.08 秒 |
| 固定 Terra | Terra | 0% | 否 | 否 | 152.37 秒 |
| 固定 Sol | Sol | 100% | 是 | 是 | 255.95 秒 |

处理耗时采用原始 `TTM`：从故障就绪到修复判分结束，包含路由、CLI 启动、调查、修复及 judge，不包含环境部署和清理。失败修复也保留这段时长，不能把它当作恢复时间。四组完整 main 耗时依次为 Jev 511.20 秒、Luna 435.26 秒、Terra 457.23 秒、Sol 556.75 秒。

Jev 实际返回 **Sol 0.57、Terra 0.43、Luna 0.00，confidence 0.35**，路由耗时 **0.412 秒**，按记录单价估算的 Jev API 费用为 **$0.000083958**。已核对真正启动的 Codex 使用了 Sol，judge 未被路由切换。选择概率和 confidence 不代表经过校准的解题成功概率。

| 策略 | Agent 输入 token | 其中缓存读取 | Agent 输出 token |
|---|---:|---:|---:|
| Jev→Sol | 511,076 | 444,416 | 3,568 |
| 固定 Luna | 263,407 | 224,768 | 2,638 |
| 固定 Terra | 208,958 | 180,480 | 1,770 |
| 固定 Sol | 797,623 | 733,440 | 5,853 |

缓存读取已包含在输入内。现有 judge bridge 与 preflight 没有保存 token 用量，因此它们的用量和整批美元成本为 unknown；不能把上表称为总用量，也不能用 API 参考价推算 Codex 订阅账单。

Jev→Sol 和固定 Sol 都定位到前端 Service selector 并修复。Luna 通过修改 search pod 标签恢复了正确路由，但其诊断在现有 rubric 下得分 67%，低于 70% 门槛。Terra 将根因归为 rate 服务限流，修改容量后仍未解决注入的故障。这说明诊断和修复应分开统计。

**本轮验证了真实 SREGym 流程中的路由接入，没有证明普遍收益。** 只测一个故障、每组一次，Jev 首跑且顺序未均衡；两次 Sol 使用相同模型也有不同调查轨迹和耗时，不能将 209 秒与 256 秒的差异归因于路由。当前是在任务开始前选一次模型，Jev 尚未收到诊断期间的新观测；同一应用下不同故障的初始描述可能相同，不宜直接扩成“按故障难度路由”的结论。未测试任务中途切换模型或 Responses 状态兼容性。

输入审计确认四组实际 user 任务文本、环境消息与基础指令一致，模型和 `medium` 均与记录匹配。Codex 的原生 developer 消息与 `multi_agent_version` 随模型变化：Luna 为 v1，其余为 v2；完整上下文和工具 schema 不能视为逐字一致，本轮未观察到实际子 agent 委派。`svelte` 结果不应当作 `full` 配置榜单成绩。

控制器遇到两个记录层问题，均保留原记录并以独立证据解除：重复构建改变了 attestation/index，但实际配置和 runtime manifest 相同；最后的全局 Docker 扫描捕获了 benchmark 清理结束后才出现的无关容器。没有重跑任何模型或更改评分。四组结束后，已修正后续 pilot 的镜像内容比较和容器检查范围。

实验主体来自 commit `b73d8ac`，应用子模块为 `887d093e4cb7ff90e5a37dd2f11320d70d2e47a7`。603 个源码/配置文件及原控制器均有冻结快照；最终输入审计在后续 controller 修复前确认全部哈希一致。

[原始结果、时长拆分与审计](../../results/jev-pilot-20260922T031520.700332Z/RESULTS.zh.md) · [四组完成记录](../../results/jev-pilot-20260922T031520.700332Z/completion.json) · [最终输入审计](../../results/jev-pilot-20260922T031520.700332Z/final-input-audit.json) · [复现协议](PROTOCOL.md)

原始运行数据保存在本机 `results/`，不随 Git 提交。路由入口是 `main.py --model-router jev`，默认关闭；需要显式固定 judge。四组复现使用 `scripts/run_jev_pilot.py --prompt-key`，原生订阅凭据由既有 SREGym 流程管理，TypeSafe key 只在控制端使用。
