# Baseline agent on SREGym-Lite

One run of the `baseline` agent over all 21 SREGym-Lite problems, both stages, on
2026-09-18. Kept here so the leaderboard numbers below can be checked against the
transcripts that produced them.

## Configuration

| | |
| --- | --- |
| Agent | `baseline` (mini protocol, unrestricted, model submits itself by curl) |
| Model | `openai/deepseek-flash`, reasoning effort `max` |
| Judge | `gpt-5.6-sol` at effort `high`, codex backend |
| Deployment profile | `svelte` |
| Limits | 80 commands and 1500 seconds per stage, 60 seconds per command |
| Attempts | 1 per problem |

```
python main.py --suite sregym-lite --agent baseline --model openai/deepseek-flash \
  --reasoning-effort max --judge-backend codex --judge-model gpt-5.6-sol \
  --profile svelte --n-attempts 1 --force-build --agent-timeout 3600
```

## Results

| | | |
| --- | ---: | --- |
| Diagnosis | 17/21 | 81.0% |
| Mitigation | 18/21 | 85.7% |
| End-to-end | 15/21 | 71.4% |

Time to diagnose: 481 s median, 578 s mean. Time to mitigate: 667 s median, 904 s
mean. 2.06M tokens per problem on average, 97% of the input served from cache.
Every one of the 42 stages ended with a submission the model made itself; one
stage reached the 80-command cap and four came within fifteen of it.

| Problem | Diagnosis | Mitigation | TTD (s) | TTM (s) | Run |
| --- | ---: | :---: | ---: | ---: | --- |
| `admission_webhook_outage_hotel_reservation` | 100 | pass | 655 | 810 | rerun |
| `cronjob_sidecar_blocks_completion_hotel_reservation` | 100 | fail | 1463 | 1889 | suite |
| `duplicate_pvc_mounts_social_network` | 0 | fail | 1279 | 2826 | rerun |
| `edge_request_filter_cpu_saturation` | 89 | pass | 506 | 667 | suite |
| `env_variable_shadowing_astronomy_shop` | 100 | pass | 230 | 395 | suite |
| `finalizer_deadlock_controller_hotel_reservation` | 100 | pass | 537 | 582 | suite |
| `internal_traffic_policy_local_astronomy_shop` | 56 | pass | 370 | 469 | suite |
| `kafka_poison_pill_hol_block` | 89 | fail | 468 | 924 | suite |
| `mutating_webhook_resource_limits_social_network` | 44 | pass | 121 | 388 | suite |
| `namespace_memory_limit` | 100 | pass | 593 | 847 | suite |
| `network_policy_block` | 100 | pass | 481 | 598 | suite |
| `readiness_probe_misconfiguration_social_network` | 100 | pass | 132 | 257 | suite |
| `rolling_update_misconfigured_social_network` | 100 | pass | 956 | 1131 | rerun |
| `search_rate_retry_collapse_hotel_reservation` | 67 | pass | 1309 | 1462 | suite |
| `secret_rotation_stale_env_credentials_astronomy_shop` | 100 | pass | 766 | 931 | suite |
| `service_dns_resolution_failure_social_network` | 89 | pass | 383 | 501 | suite |
| `service_wrong_pod_selection_hotel_reservation` | 100 | pass | 406 | 513 | suite |
| `unschedulable_incorrect_port_assignment` | 89 | pass | 59 | 383 | suite |
| `valkey_auth_disruption` | 89 | pass | 1138 | 1673 | suite |
| `wrong_dns_policy_astronomy_shop` | 100 | pass | 56 | 175 | suite |
| `wrong_service_selector_social_network` | 100 | pass | 226 | 1557 | rerun |
Four problems were rerun on the same configuration after the host's egress DNS
failed mid-run (three of them) and after the model fabricated its own command
output and tripped the format check three times (`rolling_update_misconfigured`,
which passed on the rerun). `Run` says which attempt each row comes from. The
superseded attempts are not part of this data set.

## Files

Per problem, under `runs/<problem>/`:

| File | Contents |
| --- | --- |
| `baseline_transcript.jsonl` | every model call and every command, with return code, timings and output |
| `trajectory.json` | the same run in ATIF v1.7, including each step's reasoning |
| `results.json` | the driver's own record: usage, command counts, how each stage ended |
| `scored.csv` | the conductor's row: the judge's nine questions, dimension scores, submission text |

`lite21_max_ALL_results.csv` joins the scored rows for all 21 problems.

The driver also writes `steps/step_NN/` with the exact message list sent at each
step. It is 380 MB for this run and reconstructible from the transcript, so it is
not kept here; the per-step reasoning it holds is in `trajectory.json`.
