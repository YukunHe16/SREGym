# SREGym 单任务四策略订阅试跑

本协议在生成前固定。目标是验证完整 SREGym 故障部署、模型选择、诊断提交、实际修复和独立判分是否可以衔接，并记录单次运行的质量、时间和用量。一个故障、每策略一次的结果不能证明普遍路由收益。

## 固定任务和执行顺序

- 任务为 `service_wrong_pod_selection_hotel_reservation`，部署配置为 `svelte`。使用该任务默认的完整诊断和修复阶段，不传 `--stages`，不转成仅诊断任务。
- 四组是固定 `gpt-5.6-luna`、固定 `gpt-5.6-terra`、固定 `gpt-5.6-sol` 和 `--model-router jev`。每组独立启动一次 `main.py`，重新部署并注入故障，`--n-attempts 1`，agent timeout 为 900 秒。
- 先对 `[luna, terra, sol, jev]` 使用 seed `20260922` 打乱，再将 Jev 提至首位作为集成 smoke，其余三个固定模型保持打乱后的相对顺序。原始打乱顺序和实际执行顺序都在 manifest 中保存。这个安排不构成策略位置的随机均衡。
- 四组串行运行，上一 `main.py` 完全退出、结果归档和清理核查完成后才启动下一组。没有自动重跑、结果驱动换题、人工修复答案或择优选取。

## 模型、路由和判分

- 执行 agent 都是 Codex，reasoning effort 固定为 `medium`。`agents.yaml` 将容器内 agent 和 Codex judge 使用的 Codex npm 版本固定为 `0.155.1`。Mac 上用于登录管理的 CLI 版本不是此次容器执行版本。
- Jev 组命令中的 `--model gpt-5.6-terra` 仅是路由前 preflight 的配置占位。真正用于任务的模型由宿主机上的 Jev 从 Luna、Terra、Sol 中决定；应以 routing artifact 和实际 agent 启动记录核对，而不是把占位值当成路由结果。
- 路由输入只含执行者同样可以看到的初始公开应用信息及已核验的模型描述。它不含真实 problem ID、故障答案、oracle、历史运行结果或后续诊断观察。因此本次只验证任务开始前选模，不能评价根据运行中新证据切换模型的能力。
- Judge 固定为 `--judge-backend codex --judge-model gpt-5.6-sol`，与执行模型选择分开。所有组保持相同官方诊断和修复评估逻辑，不让评估者发现的信息补足执行者遗漏的诊断。
- `filtered` 网络政策和容器 hardening `on` 保持启用；不为试跑放开网络或容器权限。`svelte` 改变可观察环境，其结果不能当作 `full` 配置榜单成绩。

## 凭据、镜像与环境记录

- 使用现有 Codex 订阅登录供 agent 和 judge 使用，不引入 OpenAI API 计费。父控制器通过 `--prompt-key` 无回显读取一次 Jev key，仅保留于内存，并只传给 Jev 那次 `main.py` 的宿主进程环境。固定模型组环境中没有 Jev key。
- 控制器采用环境白名单，移除 `OPENAI`、`AGENT`、`JUDGE`、Anthropic 等 API key/base 配置以及继承的 judge bridge URL。凭据不进入命令行、manifest、冻结配置或控制器输出；主进程合并日志额外对 Jev key 脱敏。现有原生订阅认证仍由 SREGym 的授权挂载处理。
- 明确使用 `/Users/yukun/Documents/ChatGPT/research/output/sregym-lite-local-20260916/kubeconfig`。Python 复用 `/Users/yukun/Documents/SREGym-lite-local/.venv/bin/python`，不修改原仓库或该环境的依赖。
- 四组都使用 `--force-build`，复用 Docker 缓存并构建同一 `sregym-agent-base:latest` 标签。原因是每个新进程默认会选择已发布镜像 digest，仅首组 force-build 会导致后续组回到旧镜像。每次记录实际镜像 ID，不把相同标签当作相同镜像字节的证据。
- 记录工作树 commit、分支、修改状态、应用子模块 commit/状态、Python 与包版本、Docker 信息、Kubernetes context 和节点资源，以及运行源代码和配置 hash/快照。每组启动前检查冻结代码是否变化。

## 结果保存与停止条件

- 结果保存在独立 `results/jev-pilot-<UTC>/`，每组有命令、合并日志、退出码、完整 main 耗时、CSV、原始 benchmark artifacts 和新建 judge 日志。
- `main.py` 原生结果目录只有分钟精度。控制器拒绝在已有这种目录的情况下启动；每组主进程完整退出后，将该次新目录移动到 `<pilot>/<arm>/benchmark-results/`，再运行下一组，避免同一分钟覆盖。CSV 链接和原路径记录在组结果中。
- 第一组 Jev 同时验证路由、执行、判分和清理。main 非零退出、部署失败、缺少完整阶段结果、oracle 异常、环境或 harness 类错误、路由失败、归档失败或清理不确定时停止后续组，保留已有证据，不静默替换模型或重试。
- 一次完整诊断或修复判为 `success=False`，只要有完整判分且没有评估器错误，就作为有效模型结果继续，不将普通错误答案归类为基础设施失败。未完成任务单独记录，不能伪装成已判分的零分。
- 900 秒限制只约束 main 中的 agent 阶段，部署、镜像构建、judge 和清理有各自耗时。父控制器记录完整 main wall time；比较时还应使用 phase ledger 区分路由、agent、judge、部署和清理，不把初始镜像构建开销解释为模型速度。
- Codex 订阅用量与逐任务美元账单分开。记录可取得的 token/cache 计数；未知用量保持未知。Jev 费用依其返回的真实输入 token 估算。Judge/preflight 消耗也应单列，不能只统计执行模型而声称总成本。

## 启动方式

在完成静态检查、镜像内容检查及集群就绪核查后，于交互终端运行：

```bash
cd /Users/yukun/Documents/SREGym-jev-routing
/Users/yukun/Documents/SREGym-lite-local/.venv/bin/python scripts/run_jev_pilot.py --prompt-key
```

这条命令会实际部署任务并调用模型；本协议与脚本的交付本身不代表已经执行或通过该试跑。
