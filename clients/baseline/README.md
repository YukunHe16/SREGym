# Baseline agent

A small agent for SREGym. It works the same way as [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)
v1.17.5. In each reply the model writes one bash command. The agent runs the command in a new shell
and sends back the exit code and the output. Any model that LiteLLM can call with an API key works.

## Running

```
export AGENT_API_BASE=https://api.example.com/v1   # your provider's endpoint
export AGENT_API_KEY=...                           # your provider's key
uv run main.py --suite sregym-lite --agent baseline --model openai/<model> \
  --force-build --internet-access open
```

- `--force-build` builds the agent image from your local code. The released image does not have
this agent, so without the flag the run fails with "No module named 'clients.baseline'".
- `--internet-access open` is needed for now. In filtered mode SREGym only allows the model
providers of the other agents, so the run stops before it starts.
- `--reasoning-effort` is passed to the model as `reasoning_effort`. If you leave it out, the
provider's default is used.

## Prompt

### System prompt

This is the mini-swe-agent system prompt. It is not changed. mini-swe-agent is under the MIT
License, which is in `LICENSE-mini-swe-agent`.

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

### First message

The first message has three parts.

1. The SREGym task text. The text comes from `build_instruction` in `clients/codex/driver.py`.
2. One line saying that every command runs in a new subshell.
3. Five rules on how to work. They replace the six steps for fixing code in mini-swe-agent.

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

### After each command

The model gets the exit code, the output, and a budget line like this:

```
<budget>command 3 of 80, about 23 minutes left in this stage</budget>
```

If the output has 10,000 characters or more, the model only sees the first 5,000 and the last
5,000. mini-swe-agent does the same. When 15 commands or 5 minutes are left, the budget line is
replaced by a message that tells the model to submit now. `mini.py` has the text of all these
messages. It also has the messages for a reply in the wrong format, a command that timed out, and
a stage that hit its limit.

## Limits and settings

Each stage allows 80 commands and 1500 seconds. Each command can run for 60 seconds. When a stage
hits a limit, the model is told once to submit and gets 3 more replies. If it still does not
submit, the stage ends without a submission. A stage also ends without a submission after 3
replies in a row that do not have exactly one bash block, or after 2 failed model calls in a row.

The agent runs inside the agent container, so environment variables from your shell do not reach
it. Set them in `agents.yaml` under `kickoff_env`:


| Variable                   | Default | Meaning                                                                   |
| -------------------------- | ------- | ------------------------------------------------------------------------- |
| `BASELINE_HARD_CAP`        | 80      | Commands per stage                                                        |
| `BASELINE_DEADLINE_S`      | 1500    | Seconds per stage                                                         |
| `BASELINE_COMMAND_TIMEOUT` | 60      | Seconds per command                                                       |
| `BASELINE_WRAP_UP_CALLS`   | 3       | Replies allowed after the limit message                                   |
| `BASELINE_MAX_TOKENS`      | 65536   | `max_tokens` for each model call                                          |
| `BASELINE_EXTRA_BODY`      | none    | Extra JSON added to every request, for options that only one provider has |
| `BASELINE_TEMPERATURE`     | none    | Sampling temperature. If unset, the provider's default is used            |


## Differences from mini-swe-agent


|                           | mini-swe-agent                                 | Baseline agent                                     |
| ------------------------- | ---------------------------------------------- | -------------------------------------------------- |
| How the model submits     | Prints a marker line                           | Sends a POST to `/submit`, as the task text says   |
| How to work               | Six steps for fixing code                      | Five rules for investigating                       |
| Budget                    | Not shown                                      | Shown after every command                          |
| Limits                    | No step limit, $3 cost limit, 30 s per command | 80 commands and 1500 s per stage, 60 s per command |
| Past reasoning            | Not sent back to the model                     | Sent back, if the provider returns it              |
| Reply with only reasoning | Treated as a format error                      | The reasoning is used as the reply                 |
| stderr                    | Mixed with stdout in order                     | Added after stdout                                 |
| 3 format errors in a row  | The run continues                              | The stage ends                                     |


## Files

The agent writes these files in the run's log folder:


| File                        | Contents                                                                               |
| --------------------------- | -------------------------------------------------------------------------------------- |
| `baseline_transcript.jsonl` | The prompt, every model call, and every command with its exit code, timing, and output |
| `baseline_results_*.json`   | Token usage, command counts, and how each stage ended                                  |
| `steps/step_NN/`            | The exact messages sent at each step, the reply, and its reasoning                     |


SREGym writes its other files as it does for every agent, including `trajectory.json` (ATIF v1.7).
The first two steps in `trajectory.json` are the system prompt and the first message.
