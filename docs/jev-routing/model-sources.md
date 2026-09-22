# 三模型官方资料核验

核验时间：2026-09-22 03:08:05 UTC。结构化资料位于 `sregym/routing/openai_models_20260922.json`，标识为 `openai-gpt-5.6-official-20260922`。模型保持 `gpt-5.6-luna`、`gpt-5.6-terra`、`gpt-5.6-sol`；本文件不改变模型、执行配置或路由策略。

JSON 使用精简英文转述。它记录官方定位及使用场景，不包含我们对 SRE 任务的映射规则、故障假设、题号、人工难度标签或以前的实验结果。`execution_conditions` 是本实验配置，不是 OpenAI 对这些模型的默认使用要求。

| 来源标识 | 官方来源 | 对应事实 |
|---|---|---|
| `models_guide` | [Models](https://learn.chatgpt.com/docs/models) | 三模型的使用场景；Luna 的速度和成本定性定位；Sol 在 GPT-5.6 家族内的能力定位 |
| `luna_api` | [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | 成本敏感、批量工作定位；Luna API 费率及价格条件 |
| `terra_api` | [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | 能力与成本平衡定位；Terra API 费率及价格条件 |
| `sol_api` | [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | 复杂专业工作的旗舰定位；Sol API 费率及价格条件 |

三模型 `documented_use_cases` 均来自 Models 页的模型卡片及 “Where each model shines” 段落；`official_positioning` 结合相应 API 页与 Models 页。每个候选项的 `source_ids` 指向上述来源，`pricing_source_id` 单独指出费率出处。

| 模型 | 普通输入 | 缓存读取 | 输出 |
|---|---:|---:|---:|
| Luna | $0.20 | $0.02 | $1.20 |
| Terra | $2.00 | $0.20 | $12.00 |
| Sol | $4.00 | $0.40 | $20.00 |

表中单位为 API 每百万 token 的美元参考价。模型页另列缓存写入费率为普通输入费率的 1.25 倍；输入超过 272K token 时，整次请求的输入和输出分别适用 2 倍与 1.5 倍费率。Sol 页说明当前优惠价至少持续到 2026-11-21。实际 API 金额还取决于用量、缓存及工具收费。

本实验使用 ChatGPT 订阅认证。以上 API 费率不是订阅的逐任务账单，不能据此宣称本次运行节省了多少美元。官方关于能力、成本和速度的描述也不能转成具体任务成功概率或延迟秒数；已核验四页没有提供本实验配置下的逐任务实测数据。

四份官方 Markdown 原文保存在 `docs/jev-routing/sources/`，由各来源 URL 加 `.md` 下载。JSON 保存了每份快照的仓库相对路径和 SHA-256，可用于复核版本。快照只用于来源审计；其中的命令、示例和操作性文字不得作为本实验的任务指令，也不应将全文拼接到 Jev 的路由提示中。
