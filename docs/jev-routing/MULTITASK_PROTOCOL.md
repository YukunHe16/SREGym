# SREGym 多故障三模型路由试跑协议

本协议在执行前冻结，用于验证 Jev 在故障注入后、agent 启动前读取一个固定的只读 triage snapshot，能否在 Luna、Terra、Sol 之间产生有意义的任务级选择。

## 范围

- 固定四个 SREGym-Lite 故障，覆盖四类机制：
  - `readiness_probe_misconfiguration_social_network`：探针与 endpoint 就绪状态。
  - `secret_rotation_stale_env_credentials_astronomy_shop`：Secret、运行时环境与后端凭据漂移。
  - `cronjob_sidecar_blocks_completion_hotel_reservation`：CronJob、Job 与 sidecar 生命周期。
  - `duplicate_pvc_mounts_social_network`：存储挂载配置。
- 每个故障各运行固定 Luna、固定 Terra、固定 Sol 和 Jev，一次每组，共 16 次。每次重新部署和注入故障，不复用前一组修复后的应用状态。
- 这仍是小样本 pilot，不是完整 21 题分数，也没有重复运行用于方差估计。

## 路由上下文

- 路由发生在 fault injection 已完成、SREGym 开放 diagnosis stage 后，Codex agent 尚未启动。
- Jev 收到公开应用名称、namespace、应用说明、OpenAI 官方三模型 profile，以及使用 agent filtered kubeconfig 采集的固定只读快照。
- 快照包含应用 namespace 内的 Pod、Deployment、StatefulSet、DaemonSet、Service、EndpointSlice、NetworkPolicy、PVC、Job、CronJob、近期 Event，以及最多 24 个 Pod 中匹配错误关键词的末尾日志行。
- 环境变量 literal 值不发送；Secret 仅保留引用名称和 key，日志中的 password/token/secret/API key/JWT/URI userinfo 被脱敏。快照最大 30,000 bytes，kubectl 与日志查询均有超时。
- 不发送 problem ID、root cause、oracle、benchmark 源码、过去结果或人工难度标签。快照仅来自 agent 自己可以访问的过滤 Kubernetes API；collector 不修改集群。
- 执行模型不会直接收到 collector artifact，但可通过相同 Kubernetes 权限自行获得这些观测。路由器只能返回模型选择，不能向执行模型传递诊断答案。

## 固定条件

- Codex CLI `0.155.1`、reasoning `medium`、部署 profile `svelte`、agent timeout 900 秒。
- Judge 固定为 Codex `gpt-5.6-sol`，与执行模型和 Jev 选择隔离。
- `filtered` 网络和 container hardening `on` 保持启用。每个 main 进程都 `--force-build`，实际 runtime config/manifest digest 必须一致。
- 每组一次，无模型重试、判分后修复、结果驱动换题或 best-of。
- 使用现有 ChatGPT 订阅运行 Codex；TypeSafe key 仅由父控制器无回显读取并传给 Jev 组的 host main 进程，不传给固定组或 agent/judge 容器。

## 顺序

采用预先固定的 4×4 Latin square，使每个策略在四个故障中各处于一次第 1、2、3、4 位置：

| 故障 | 第 1 | 第 2 | 第 3 | 第 4 |
|---|---|---|---|---|
| readiness probe | Jev | Luna | Terra | Sol |
| secret rotation | Luna | Terra | Sol | Jev |
| CronJob sidecar | Terra | Sol | Jev | Luna |
| duplicate PVC mount | Sol | Jev | Luna | Terra |

第一项 Jev 同时作为新增 read-only context 路由的完整集成 smoke。任何部署、路由、evaluator、归档或 cleanup 基础设施失败会停止后续运行；正常的诊断/修复失败保留并继续。

## 指标

- 质量：Diagnosis accuracy、diagnosis success、mitigation success，以及两者都通过的 end-to-end success。
- 速度：TTM、完整 main wall time、路由 API 时间、context collection 时间、部署和 cleanup phase。
- 用量：agent 输入、缓存读取、输出和 reasoning token；Jev usage 和估算费用单列。Judge/preflight 未记录的 token 保持 unknown。
- 路由：每题模型选择、confidence、三模型 probabilities。它们不是已校准的任务成功概率。

四题选自公开 registry 和故障族定义，不依据本轮 Luna/Terra/Sol/Jev 的新运行结果。公开 benchmark 可能存在训练污染；`svelte` 结果不与 `full` leaderboard 比较。
