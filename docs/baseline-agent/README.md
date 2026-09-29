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

The model sees plain text only. Each reply must hold one bash command. Each command runs in
a fresh shell, and its output comes back as the next message. Below is everything the model
is sent, in order. The texts are in `clients/baseline/mini.py`.

### 1. System prompt

The system prompt of mini-swe-agent, unchanged:

````
You are a helpful assistant that can interact with a computer.
Your response must contain exactly ONE bash code block with ONE command (or commands connected with && or ||).
Include a THOUGHT section before your command where you explain your reasoning process.
Format your response as shown in <format_example>.

<format_example>
Your reasoning and analysis here. Explain why you want to perform the action.

```bash
your_command_here
```
</format_example>

Failure to follow these rules will cause your response to be rejected.
````

### 2. First message

Three parts, one after the other.

**The task text.** This is the text SREGym gives the Codex agent (`build_instruction` in
`clients/codex/driver.py`), unchanged. It names the application and the namespace,
describes the two stages, and says how to submit: a POST to the conductor's `/submit`.

<details>
<summary>The task text, as sent for astronomy-shop</summary>

```
You are an SRE agent tasked with diagnosing and fixing issues in a Kubernetes application.

Application: OpenTelemetry Demo Astronomy Shop
Namespace: astronomy-shop

An online shopping platform built with a microservices architecture, showcasing OpenTelemetry instrumentation for distributed tracing across services.

CRITICAL: You are running in an AUTOMATED environment. Work autonomously and make all decisions yourself. DO NOT ask for user confirmation or approval. Proceed with the best solution based on your analysis.

WORKFLOW: You will perform TWO tasks in sequence:

TASK 1: DIAGNOSIS
- Investigate the application to detect any anomalies or issues
- Analyze metrics, logs, and traces
- When ready, submit a natural language description of the issue you found
- Your diagnosis is evaluated on whether you correctly identify the faulty components and root cause

TASK 2: MITIGATION
- Identify the root cause of the issue
- Implement a fix to resolve the problem autonomously (do not ask for confirmation)
- After applying the fix, YOU MUST submit with an empty string to trigger validation
- The submission is REQUIRED - do not exit without submitting
- Your mitigation is evaluated on whether the application is healthy after your changes
- Your fix is also evaluated on whether it addresses the root cause, not just the symptoms

HOW TO SUBMIT:

The submission endpoint is: http://host.docker.internal:8000/submit

For DIAGNOSIS stage:
- Submit with a natural language description of the issue
- Example: POST http://host.docker.internal:8000/submit with JSON: {"solution": "The frontend service is crashing due to missing environment variable"}

For MITIGATION stage:
- After applying your fix, YOU MUST submit with an EMPTY STRING
- POST http://host.docker.internal:8000/submit with JSON: {"solution": ""}
- This submission is MANDATORY - the conductor needs it to validate your fix

Important:
- You have access to kubectl commands to inspect and modify resources in namespace 'astronomy-shop'
- You can query metrics and traces through the available observability tools
- The conductor API is available at http://host.docker.internal:8000
```

</details>

**A note about the shell:**

```
Note: every command is executed in a new subshell; directory or environment variable changes do not persist.
```

**Five rules on how to work.** They take the place of mini-swe-agent's six steps for fixing
code. They say how to investigate, never where to look. They name no kind of Kubernetes
object, no kind of fault and nothing the judge scores.

```
## How to work

1. Before each command, say what you currently think is wrong and what this command would tell you.
   A command whose result cannot change your mind is not worth running.
2. Read the output you asked for before asking for more, and say what it rules in and what it rules out.
3. If two commands in a row tell you nothing new about the current idea, drop it and try another one.
4. Survey the whole system before going deep into any one part of it.
5. Stop as soon as your evidence answers the task. Evidence gathered after that point cannot improve your
   answer, and the stage ends whether or not you have submitted.
```

### 3. After each command

```
<returncode>0</returncode>
<output>
(the command's output)
</output>
<budget>command 3 of 80, about 23 minutes left in this stage</budget>
```

An output of 10,000 characters or more is cut to its first and last 5,000, with a warning,
as in mini-swe-agent. In the last 15 commands, or the last 5 minutes, the budget line is
replaced by a notice:

```
<IMPORTANT>
You have 10 commands left in this stage. When that runs out the stage ends, and if you have not submitted by then,
nothing is recorded for it. Stop pulling on whatever thread you are on and come back to the big picture:
submit your best answer now. Submit it to the conductor exactly as the task instruction describes.
</IMPORTANT>
```

### 4. Other fixed messages

- A reply without exactly one bash block gets mini-swe-agent's format message: "Please
  always provide EXACTLY ONE action in triple backticks, found 2 actions. ..."
- A command still running after 60 seconds is killed, and the model gets mini-swe-agent's
  timeout message.
- At the command limit or the time limit the model is told once: "You have reached the
  limit of 80 commands for this stage. Stop investigating and submit your best answer now,
  based on what you already know. Submit it to the conductor exactly as the task
  instruction describes."

Nothing is sent when the diagnosis stage ends. The mitigation stage goes on in the same
conversation, since the task text already describes both stages.

## How it differs from mini-swe-agent and from Codex

| | mini-swe-agent | baseline agent | Codex agent in SREGym |
| --- | --- | --- | --- |
| System prompt | its own | the same as mini-swe-agent | the one built into the Codex CLI; SREGym does not change it |
| Task | the task, rules on the reply format, six steps for fixing code | SREGym's task text, a note about the shell, five rules for investigating | SREGym's task text only |
| Tools | one bash command per reply, each in a fresh shell | the same | the Codex CLI's own tools; SREGym turns on `unified_exec`, which keeps a shell running between commands |
| How a stage ends | a command prints a marker line | the model posts to `/submit` itself, as the task text says | the same as the baseline agent |
| Budget | not shown | commands used and minutes left, after every command | not shown |
| Limits | no step limit, 3 dollars, 30 s per command | 80 commands and 1500 s per stage, 60 s per command | no command limit; SREGym's `--agent-timeout` ends the run |
| A long conversation | sent whole | sent whole; a stage that fills the model's window ends | the Codex CLI has its own setting to compact it |
| Earlier reasoning | not sent back | sent back with each reply, when the provider returns it | handled by the Codex CLI |
| A reply with only reasoning | a format error | the reasoning is read as the reply | handled by the Codex CLI |
| stderr | mixed into stdout in order | put after stdout | handled by the Codex CLI |
| Three format errors in a row | keeps going | the stage ends | does not apply |
| Reasoning effort | set in its model config | `--reasoning-effort`, sent as `reasoning_effort` | `--reasoning-effort`, sent as `model_reasoning_effort` |

## Limits

Each stage has 80 commands and 1500 seconds, and each command 60 seconds. At a limit the
model is asked once to submit and gets three more replies. If it still has not submitted,
the stage ends with no submission, like a CLI agent that stops. The same happens after
three replies in a row without exactly one bash block, or two failed model calls in a row.

These can be changed in `agents.yaml` under `kickoff_env`: `BASELINE_HARD_CAP`,
`BASELINE_DEADLINE_S`, `BASELINE_COMMAND_TIMEOUT`.

## Files it writes

The agent writes these in the run's log folder:

| File | Contents |
| --- | --- |
| `baseline_transcript.jsonl` | the prompt, every model call and every command, with return code, timings and output |
| `baseline_results_*.json` | usage, command counts, how each stage ended |
| `steps/step_NN/` | the exact messages sent at each step, the reply and its reasoning |

These take the place of a CLI agent's own logs (for Codex, `sessions/` and `codex.txt`).

SREGym writes the rest, as it does for every agent: `trajectory.json` (the run in ATIF
v1.7), `driver.log`, `driver.rc`, `<problem>_results.csv` (the judge's scores) and its own
`sregym_*.log`. As in the other agents' trajectories, the first steps of `trajectory.json`
are the prompt: the system prompt, then the first message. Runs made before the agent
recorded its prompt (the ones in `lite21-qwen/`) start with the model's first reply.
