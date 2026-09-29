# Baseline agent on SREGym-Lite, local Qwen

One run of the `baseline` agent over the 21 SREGym-Lite problems, both stages, on
2026-09-28, with a model served on the same machine. Kept here so the numbers below can
be checked against the transcripts that produced them.

## Configuration

| | |
| --- | --- |
| Agent | `baseline` (this branch; see the note on the code below) |
| Model | Qwen3.8-27B with multi-token prediction, served by MTPLX on the host, reasoning effort not set |
| Context window | 65,536 tokens; 131,072 for the four problems rerun (see below) |
| Judge | `gpt-5.6-sol`, codex backend, effort not set (the model's default, `low` in Codex's model list) |
| Deployment profile | `svelte` |
| Limits | 80 commands and 1500 seconds per stage, 60 seconds per command |
| Attempts | 1 per problem |

```
export AGENT_API_BASE=http://127.0.0.1:18081/v1 AGENT_API_KEY=mtp-local
python main.py --suite sregym-lite --agent baseline --model openai/qwen3.8-27b-mtp \
  --judge-backend codex --judge-model gpt-5.6-sol --profile svelte --n-attempts 1 \
  --agent-timeout 3600 --force-build --internet-access open
```

`--profile svelte` leaves out components nothing in SREGym reads, to fit the run in
the machine's memory. SREGym's README says its scores are not comparable with the default
profile, so these numbers are not a leaderboard entry.

## The four problems rerun at 128K

In the 64K run four problems filled the context window before a stage ended:
`duplicate_pvc_mounts_social_network`, `finalizer_deadlock_controller_hotel_reservation`,
`kafka_poison_pill_hol_block` and `service_dns_resolution_failure_social_network`. Each was
run again, whole, with a 131,072-token window, and that run replaces the 64K one here.
None of the four filled the larger window. Only `duplicate_pvc_mounts_social_network`
needed it (104,120 tokens at most). The other three stayed under 64K the second time:
the model took a shorter path, so their result is a second try more than a larger window.

In the 64K run, `kafka_poison_pill_hol_block` passed the diagnosis. Its rerun passed
neither stage, and the rerun is what is counted.

## The code

The run was made with commit 9cf9a41d. Its baseline agent had more setups than this
branch: tool calling, command budgets, a three-field diagnosis. The run used none of them.
On the setup it used, every text the model can be shown is the same in both versions,
and a scripted replay of both stages gives the same conversation. The records of the run
carry a few fields this branch no longer writes (`refused`, `submission_attempt`,
`tool_counts`, `max_commands` and similar); they are always empty or fixed.

## Results

| | | |
| --- | ---: | --- |
| Diagnosis | 12/21 | 57.1% |
| Mitigation | 13/21 | 61.9% |
| End-to-end | 11/21 | 52.4% |

Time to diagnose: 392 s median, 339 s mean. Time to mitigate: 417 s median, 442 s mean.
1.44M tokens per problem on average, 97.7% of the input served from the server's cache.

| Problem | Diagnosis | Mitigation | TTD (s) | TTM (s) | Window |
| --- | ---: | :---: | ---: | ---: | --- |
| `admission_webhook_outage_hotel_reservation` | 100 | pass | 80 | 113 | 64K |
| `cronjob_sidecar_blocks_completion_hotel_reservation` | 0 | fail | 400 | 480 | 64K |
| `duplicate_pvc_mounts_social_network` | 0 | fail | 562 | 1522 | 128K |
| `edge_request_filter_cpu_saturation` | 78 | pass | 578 | 608 | 64K |
| `env_variable_shadowing_astronomy_shop` | 78 | pass | 345 | 417 | 64K |
| `finalizer_deadlock_controller_hotel_reservation` | 100 | pass | 392 | 414 | 128K |
| `internal_traffic_policy_local_astronomy_shop` | 0 | fail | 423 | 442 | 64K |
| `kafka_poison_pill_hol_block` | 0 | fail | 458 | 795 | 128K |
| `mutating_webhook_resource_limits_social_network` | 78 | pass | 225 | 318 | 64K |
| `namespace_memory_limit` | 89 | fail | 99 | 117 | 64K |
| `network_policy_block` | 100 | pass | 680 | 725 | 64K |
| `readiness_probe_misconfiguration_social_network` | 100 | pass | 109 | 155 | 64K |
| `rolling_update_misconfigured_social_network` | 67 | fail | 87 | 129 | 64K |
| `search_rate_retry_collapse_hotel_reservation` | 34 | fail | 263 | 281 | 64K |
| `secret_rotation_stale_env_credentials_astronomy_shop` | 33 | pass | 596 | 764 | 64K |
| `service_dns_resolution_failure_social_network` | 100 | pass | 479 | 504 | 128K |
| `service_wrong_pod_selection_hotel_reservation` | 34 | pass | 442 | 474 | 64K |
| `unschedulable_incorrect_port_assignment` | 55 | fail | 95 | 120 | 64K |
| `valkey_auth_disruption` | 100 | pass | 442 | 458 | 64K |
| `wrong_dns_policy_astronomy_shop` | 100 | pass | 71 | 101 | 64K |
| `wrong_service_selector_social_network` | 100 | pass | 287 | 354 | 64K |

Diagnosis is the judge's composite score out of 100; 70 or more passes (SREGym's default threshold).

## Files

Per problem, under `runs/<problem>/`:

| File | Contents |
| --- | --- |
| `baseline_transcript.jsonl` | every model call and every command, with return code, timings and output |
| `trajectory.json` | the same run in ATIF v1.7, including each step's reasoning |
| `results.json` | the driver's own record: usage, command counts, how each stage ended |
| `scored.csv` | the conductor's row: the judge's questions, dimension scores, submission text |

`lite21_qwen_ALL_results.csv` joins the scored rows for all 21 problems.

The driver also writes `steps/step_NN/` with the exact message list sent at each step.
It is not kept here; the per-step reasoning it holds is in `trajectory.json`.
