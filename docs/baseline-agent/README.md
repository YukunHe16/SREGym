# Baseline agent

`clients/baseline` is a small reference agent for SREGym. It follows the
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) recipe (v1.17.5): the model
writes one bash command per reply, the command runs in a fresh shell, and the model sees
the return code and the output. It works with any model LiteLLM can reach with an API key.

## Running it

```
export AGENT_API_BASE=https://api.example.com/v1   # your provider's endpoint
export AGENT_API_KEY=...                           # your provider's key
python main.py --suite sregym-lite --agent baseline --model openai/<model> \
  --judge-backend codex --judge-model gpt-5.6-sol --profile svelte \
  --n-attempts 1 --agent-timeout 3600 --force-build --internet-access open
```

`--force-build` builds the agent image from your checkout. The released image has no
baseline agent in it, so without this flag the run stops with "No module named
'clients.baseline'".

`--internet-access open` is needed on SREGym versions that filter the agent's internet
access. The filter only knows the model providers of the other agents, so for the
baseline agent it stops the run before it starts.

`--reasoning-effort` (for example `high`) is passed to the model as `reasoning_effort`.
Leave it out to use the provider's default. The agent sends nothing else that is specific
to one provider.

## What the model gets

- The system prompt of mini-swe-agent: one bash block per reply, a short thought before it.
- The task text every CLI agent gets (`clients/codex/driver.py`), unchanged. It tells the
  model to submit with `curl` to the conductor, and the model does that itself.
- A note that each command runs in a new shell, and five general rules on how to
  investigate (`WORKFLOW_TEXT` in `clients/baseline/mini.py`). They say how to work, never
  where to look.
- After each command: the return code and the output. An output over 10,000 characters is
  cut to its first and last 5,000, as in mini-swe-agent.
- After each command, a line with the commands used and the minutes left. In the last 15
  commands or the last 5 minutes it becomes a notice to submit.

## Limits

Each stage has 80 commands and 1500 seconds, and each command 60 seconds. At a limit the
model is asked once to submit and gets three more replies. If it still has not submitted,
the stage ends with no submission, like a CLI agent that stops. The same happens after
three replies in a row without exactly one bash block, or two failed model calls in a row.

These can be changed in `agents.yaml` under `kickoff_env`: `BASELINE_HARD_CAP`,
`BASELINE_DEADLINE_S`, `BASELINE_COMMAND_TIMEOUT`.

## Where it differs from mini-swe-agent

| | mini-swe-agent | baseline agent |
| --- | --- | --- |
| How the task ends | a command prints a marker line | the model posts to `/submit` as the task text says |
| How to work | six steps for fixing code | five general rules for investigating |
| Budget | not shown | commands used and minutes left, after every command |
| Limits | no step limit, 3 dollars, 30 s per command | 80 commands and 1500 s per stage, 60 s per command |
| Earlier reasoning | not sent back | sent back with each reply, when the provider returns it |
| A reply with only reasoning | a format error | the reasoning is read as the reply |
| stderr | mixed into stdout in order | put after stdout |
| Three format errors in a row | keeps going | the stage ends |

## Files it writes

In the run's log folder:

| File | Contents |
| --- | --- |
| `baseline_transcript.jsonl` | every model call and every command, with return code, timings and output |
| `baseline_results_*.json` | usage, command counts, how each stage ended |
| `steps/step_NN/` | the exact messages sent at each step, the reply and its reasoning |

After each run SREGym also writes `trajectory.json`, the same run in ATIF.
