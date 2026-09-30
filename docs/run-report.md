# Run report

`sregym.results.run_report` reads runs that have already finished and says what happened in them, beyond pass or
fail. It does not touch a cluster, run an agent again or change a score. It is a first version of the tool asked for
in [#906](https://github.com/SREGym/SREGym/issues/906).

```bash
python -m sregym.results.run_report results/0918_1408/baseline --out /tmp/report
python -m sregym.results.run_report results/0918_1408/baseline --out /tmp/report \
    --labeller litellm:openai/deepseek-flash --chat-workers 16
```

The input folder is only read. Everything is written under `--out`.

[`run-report-906.md`](run-report-906.md) goes through the items of #906 one by one, with a real report as the example.

## Connecting a model

Most sections need a model. Without one (`--labeller none`, the default) only the sections made by rules are filled.
The model answers small questions about one part of the run at a time; it runs no command and sees no cluster.

There are three ways to connect a model. Whichever you use, the answers are cached in `<out>/label_cache.jsonl`. If
a build stops (a usage limit, no balance left, Ctrl-C), run the same command again: what was answered is not asked
again.

### Through an API (LiteLLM)

```bash
export LABELLER_API_KEY=...            # the provider's key
export LABELLER_API_BASE=https://...   # only for an OpenAI-compatible endpoint
python -m sregym.results.run_report results/0918_1408/baseline --out /tmp/report \
    --labeller litellm:openai/deepseek-flash --chat-workers 16
```

- `litellm:<model>` takes any model name [LiteLLM](https://docs.litellm.ai/docs/providers) accepts.
- The two variables can also be put in a `KEY=value` file given with `--key-file`. The environment wins.
- The provider bills per token. `labeller_stats.json` gives the estimated cost where LiteLLM knows the price;
  deepseek-flash came to about 0.03 to 0.07 USD per run.
- `--chat-workers` sets how many requests are sent at once (default: `--workers`, which is 6).

### Through a ChatGPT subscription (Codex CLI)

```bash
codex login                            # once, with the ChatGPT account
python -m sregym.results.run_report results/0918_1408/baseline --out /tmp/report \
    --labeller codex:gpt-6-sol
```

- Each request runs `codex exec` once, in an empty temporary folder, read-only, told to run nothing.
- It needs `codex` on `PATH`, signed in with a ChatGPT account. `OPENAI_API_KEY` and `OPENAI_BASE_URL` are removed
  from its environment so that no API key is billed.
- It counts against the subscription's usage limits and costs nothing per token.
- `--labeller-effort` sets the reasoning effort (default `medium`).

### Through a Claude subscription (Claude Code)

```bash
claude setup-token                     # once: prints a long-lived token of the subscription
export CLAUDE_CODE_OAUTH_TOKEN=...
python -m sregym.results.run_report results/0918_1408/baseline --out /tmp/report \
    --labeller claudecode:claude-sonnet-5
```

- Each request runs `claude -p` once, in an empty temporary folder, with no tools, no MCP servers, no settings files
  and nothing kept. The process gets only `HOME`, `PATH` and the token.
- The token is read from the environment, then from the `--key-file` file, then from `~/.config/sregym/claude.env`.
- It uses `claude` on `PATH`, or else the copy the Claude desktop app keeps on macOS. `CLAUDE_BIN` names another.
- It counts against the subscription's usage limits and costs nothing per token. If the subscription refuses (usage
  limit reached, token expired), the build stops; run the same command again later.
- If the model's own safeguards refuse a request, the same request is sent unchanged to `claude-sonnet-5`. The header
  of the report says how many requests were refused and who answered them.
- Give the model's full name, not an alias like `sonnet`: the name is part of the cache key and of the report.
- `--labeller-effort` is passed as `claude --effort`.

### Which model

Five models were measured on the same reference answers (see "How sure the report is" below). Each was asked once,
with the question wordings as they are now.

| Check | deepseek-flash (API) | gpt-6-sol (ChatGPT) | Claude Sonnet 5 | Claude Sonnet 5.5 | gpt-6-luna (ChatGPT) |
| --- | --- | --- | --- | --- | --- |
| Does a step name the true fault: right of 190 | 186 | 182 | 180 | 177 | 165 |
| Real namings found, of 87 | 84 | 79 | 78 | 76 | 62 |
| Was a fix attempt aimed at the fault: right of 70 | 69 | 69 | 69 | 68 | 61 |
| Real restarts found by the model alone, of 40 | 33 | 38 | 33 | 33 | 30 |
| Restarts in the report (rule and model): found of 40 / wrong | 40 / 1 | 40 / 2 | 40 / 4 | 40 / 1 | 40 / 3 |

deepseek-flash, gpt-6-sol and Claude Sonnet 5 are close. gpt-6-luna misses many namings and is not recommended.
Sonnet 5.5's safeguards refused some requests, which Sonnet 5 then answered.

### Other options

- `--labeller jev` or `jev:<model>`: [Jev](https://docs.typesafe.ai), through `clients/jev`. It needs
  `TYPESAFE_API_KEY`. It finds fewer namings and fix verdicts than the chat models above.
- `--labeller-for GROUP=SPEC` (repeatable): a different labeller for one group of questions. The groups are
  `submissions`, `outputs`, `commands`, `judge`, `answers`, `fix` and `words`. Each labeller keeps its own cache file.
- `--workers <n>` (default 6): how many requests may be sent at once. What is asked does not depend on it.
- `--lang en|zh`: the language of the Markdown reports.
- `--root-causes auto|none|<file>`: where the ground truth comes from (see "Input").

## Input

| What | Where it comes from |
| --- | --- |
| The run | `trajectory.json` (ATIF) in each run folder. SREGym converts every agent's log to ATIF, so one reader works for all agents. Whole suites have been read for the baseline agent, Codex and GitHub Copilot's CLI; for Claude Code only upstream's sample run. A `run_<n>` folder without `trajectory.json` (runs from before SREGym wrote one) is converted in memory from its session files. A tool call the reader does not know is kept as `<tool> <arguments>`, and the rules see less in it. |
| The score | `scored.csv` or `*_results.csv` in the run folder or up to two levels above it. A plain `results.csv` from another harness is read too; it has no judge checklist and no diagnosis text, and the report says so. |
| The ground truth | `--root-causes auto` (the default) reads each problem's `root_cause` from SREGym's problem definitions as they are checked out now, without a cluster. A JSON file `{"problems": {"<problem_id>": {"root_cause": "..."}}}` can be given instead, for runs made with an older definition. `none` skips it, and with it the sections that need the true fault. |

The diagnosis stage ends at the step that submitted the diagnosis.

## Output

```
<out>/<problem>/run_report.json   everything, for programs
<out>/<problem>/run_report.md     for people
<out>/summary.csv                 one row per run; joins with *_ALL_results.csv on problem_id and attempt
<out>/summary.md                  the same as a table, and the runs to read first
<out>/README.md                   how to read the reports: steps, sure and uncertain answers, every flag code
<out>/label_cache.jsonl           the model's answers, so a rebuild asks nothing and gives the same numbers
<out>/labeller_stats.json         requests, tokens, estimated cost, time taken
<out>/skipped_runs.json           runs that could not be read, and why (only if there are any)
```

## Sections

| Section | Needs a model | What it says |
| --- | --- | --- |
| Overview | no | Three sentences at the top: how the run ended, how the diagnosis went, and what the fixes did. |
| Header | no | Problem, agent, model, effort, scores, TTL/TTM, judge backend, profile, and the conductor's address from the task. |
| Messages put into the run | no | Messages that reached the agent from outside after its first reply, such as advice from an experiment or the task sent again to a new model. |
| Cost and time | no | Per stage: replies, commands, tokens, cache share, and time spent waiting for the model. |
| Submissions | optional | The diagnosis the agent sent, where it was found, and the submit commands per stage. With a model, it decides which commands really sent an answer. It also counts the restart-like commands in the mitigation stage, and how close the last one came to the final submission (#753). |
| Judge deductions | optional | Every checklist question the judge answered No, with its reason. With a model, each reason is sorted into "wrong", "adds" or "omits". |
| Suspicious paths | no | Commands that reach places where the answer can be read: the `sregym` namespace or the MCP server (#1002), the conductor beyond `/status` and `/submit`, `/opt/sregym`, oracle code, a saved baseline state. A look into `/logs` is noted without a flag. |
| Known decoys | yes | Decoys SREGym plants on purpose, and things in the application that look like the fault. For each decoy the run met: whether the agent looked at it, and what became of it. |
| Fix attempts | optional | The changes the agent made, grouped into attempts. For each: what it changed, what the checks after it showed, whether it changed what is wrong, and why the agent made it, in its own words. Then SREGym's verdict. |
| Possibly unsafe changes | optional | Fix commands that may have changed things the fault does not involve, and changes to objects outside every namespace. |
| Gaming the benchmark (in the header) | optional | A verdict: "likely", "signs to read" or "none found". Every reason names its steps. The final call stays with a person. |
| Commands | no | Every command in order, with its stage, its kind, what it changed and what other sections say about it. |
| Clues | yes | For each output of the diagnosis stage, whether it points to the true fault or elsewhere. The first clue, and how long the agent took after it. |
| The agent's own words | optional | The step where the agent first named the true fault, steps where it may have changed its mind, and the wrong suspects it followed and then dropped. |
| Where the steps went | no | Which components the commands looked at between the first clue and the naming of the fault. |
| Command audit | yes | For every command: does it look at the benchmark, reach the internet, or change the cluster. |
| What appeared on screen | yes | For every output: does it show SREGym's own material or the application's fault switches, and did it give the fault away. |
| Environment signals | yes | `OOMKilled`, `Evicted` and similar signs in the outputs, and whether the fault explains them. |

## How sure the report is

A model score of 0.7 or more counts as yes. A score from 0.5 to 0.7 is reported as uncertain. A pick-one answer counts
when its option gets at least 0.6. Answers that were often wrong are printed as "probably" or "possibly", or given as
pointers for a person to read.

The figures below compare the report with blind readers (Claude Opus models that labelled every item without seeing
the model's answers). Most were measured on runs the rules and question wordings were worked out on, so they are
somewhat optimistic.

| What the report says | How often it is right | Printed as |
| --- | --- | --- |
| A mitigation command restarts pods (rule, or the model is sure) | found 40 of 40 real restarts, 1 wrong of 41 | stated |
| What became of a decoy the agent looked at on purpose | 41 of 43 | stated, with the model's reason |
| What misled a wrong diagnosis | the same thing as the readers in 25 of 25; the same output in 19 of 24 | stated, with the model's reason |
| The checks after an attempt show it fixed | 24 of 25 | stated |
| The checks show it not fixed or made worse | 9 of 12 | "probably" |
| An attempt did not change what is wrong | 19 of 19 before the question was reworded | "probably" until measured again |
| A change reaches beyond the fault | 22 of 27, and finds all | "possibly" |
| An output points to the true fault | a yes is almost never wrong; finds 64% to 86% of real clues | stated |
| The first step that names the true fault | deepseek-flash: 49 right, 1 wrong of 51 runs | stated |
| The agent changed its mind | 4 in 9 (Jev); 10 in 11 (deepseek-flash) | pointers only |

An answer about an output can depend on which other outputs shared its request. Answers near the line are asked again
with the output on its own: asked twice with other neighbours, 87 of 8,238 answers changed sides, against 22 when
settled this way. The first naming of the true fault and the fix verdicts are also asked twice; where the two
readings differ, the report gives both.

## What is sent to the model

With a labeller, the text of the runs is sent to it: every command (the first 1,500 characters) and every command
output (the first and last 5,000 characters). The true fault is sent only with the questions that need it.
Credentials found by their shape (private keys, JWTs, API keys such as `sk-...`, AWS key ids) are masked first, in
every request, in the cache and in every report. Anything else an agent printed is sent as it is, for example a
password inside a ConfigMap. Without `--labeller`, nothing is sent anywhere.

## Cost and time

With deepseek-flash, a 21-run SREGym-Lite suite costs about 0.4 to 3 USD and takes 3 to 30 minutes. What decides it
is how much the agent wrote in its own words: an agent that writes at every step (the baseline agent) costs the most.
With gpt-6-sol through a ChatGPT subscription, a report takes a median of about 2.5 minutes, with six requests at
once. A rebuild from the cache asks nothing and takes seconds.

## Limits

- The model can be wrong. The report says how sure each part is.
- The list of known decoys is kept by hand in `sregym/results/run_report/traps.py`. A new problem with a new decoy
  needs an entry there; a test fails when a SREGym file that mentions a decoy is not listed.
- The report works from what the run recorded. A script that reads the answer and prints nothing leaves no sign.
- SREGym keeps no pod history, so environment signals are hints, not a record.
