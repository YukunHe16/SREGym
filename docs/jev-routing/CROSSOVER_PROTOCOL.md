# Jev model selection on SREGym-Lite: crossover follow-up

This follow-up tests whether Jev selects the model with better expected outcomes on faults where historical runs suggest different strengths. It is a deliberately enriched challenge set, not an estimate of overall SREGym-Lite performance.

## Frozen tasks and prior

Five candidate faults were chosen before this batch from local, single-run task-level records. Those records mixed model effort, deployment profile, judge, and run date, so they are screening hints only. They are never sent to Jev or the Codex agent. One historically easy fault is a negative control.

The local screening records are [Luna medium, svelte](../../../ChatGPT/research/output/sregym-codex-lite-20260921/full/results/0921_0021/codex_ALL_results.csv), [Terra medium](../../../ChatGPT/research/output/sregym-pial-traces-20260922/raw/SREGym-lite__gpt-5.6-terra_medium/results.csv), and [Sol max](../../../ChatGPT/research/output/sregym-pial-traces-20260922/raw/SREGym-lite__gpt-5.6-sol_max/results.csv). The latter two are downloaded trace summaries whose deployment profile and judge are not established in this protocol. Each cell below is the single historical end-to-end outcome, not a probability.

| Task | Luna medium | Terra medium | Sol max | Screening role |
| --- | :---: | :---: | :---: | --- |
| `wrong_dns_policy_astronomy_shop` | pass | fail | fail | Luna-favouring candidate |
| `internal_traffic_policy_local_astronomy_shop` | fail | fail | pass | Sol-favouring candidate |
| `finalizer_deadlock_controller_hotel_reservation` | pass | fail | fail | Luna-favouring candidate |
| `network_policy_block` | fail | pass | pass | Sol/Terra-favouring candidate |
| `wrong_service_selector_social_network` | pass | fail | fail | Luna-favouring candidate |
| `admission_webhook_outage_hotel_reservation` | pass | pass | pass | all-pass control |

The official [SREGym-Lite leaderboard](https://sregym.com/leaderboard) reports aggregate scores for its 21-fault cohort. The SREGym paper's per-problem figure covers older agent-model pairs. Neither is treated as a matched prior for this medium-effort, svelte-profile comparison.

## Execution

- Six distinct faults × three independent fresh deployments × four arms: fixed Luna, fixed Terra, fixed Sol, and Jev. Total: 72 scheduled agent runs. Each arm runs once per task-repetition block; no best-of or model retry.
- The first block is `wrong_dns_policy_astronomy_shop` repetition 1 with Jev first, providing an integration smoke. Subsequent task-repetition blocks are shuffled with fixed seed `20260922`; arm order cycles through a four-row Latin square. The complete schedule is saved before any agent starts.
- Codex CLI via the user's subscription; all execution models use reasoning effort `medium`, `svelte` profile, filtered Internet, container hardening, and a 900-second agent timeout. The diagnosis judge is fixed at Codex `gpt-5.6-sol` through the same subscription. Only Jev routing uses the TypeSafe API key and paid API; the key is entered without echo and not saved.
- Jev runs after fault injection and before agent startup. Its input is the same bounded, redacted, read-only Kubernetes snapshot and official model descriptions as the four-task pilot. It sees no problem ID, prior score, oracle, or expected answer. The selected model starts a fresh Codex session. Fixed-model arms have the same agent prompt, tools, and budget.
- Every run receives a fresh deployment and fault injection. Source hashes, runtime image config and manifest digests, command lines, result rows, and Jev decisions are archived. Infrastructure/evaluator/controller failures stop the batch for inspection. A valid 900-second model timeout is scored as failure and retained without retry. No completed model run is rerun.

## Analysis

Primary outcome: both diagnosis and mitigation pass in the same run, with model timeouts counted as failures. Report per-task and overall counts. Secondary outcomes: diagnosis accuracy and pass, mitigation pass, timeout-capped TTM, full wall time, agent token use, routing latency, selection confidence, and TypeSafe estimated spend. Missing token usage remains missing rather than zero.

Compare Jev against all three fixed arms and the best fixed arm chosen from this batch only as a descriptive upper comparator; selecting a winner on the same six tasks is optimistic. The central question is whether Jev changes selections according to fault evidence and realizes the apparent model complementarity. Any claim about general SREGym-Lite benefit requires a separately sampled, untouched task set. This batch cannot be directly compared to the official full-profile leaderboard.
