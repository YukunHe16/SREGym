# Run report: what it does for issue #906

Issue [#906](https://github.com/SREGym/SREGym/issues/906) asks for a tool that reads finished runs and tells you what
the agent did. Today people hand each `driver.log` to an AI agent and ask questions. That does not scale.

`sregym.results.run_report` is that tool. You point it at a results folder. For each run it writes a report for
people (Markdown) and the same data for scripts (JSON). For the whole folder it writes one table.

It only reads files. It does not touch a cluster, run an agent again or change a score.

## An example you can run

This repository ships one run of the baseline agent on `search_rate_retry_collapse_hotel_reservation`. The report
below was made from it with this command:

```bash
python -m sregym.results.run_report \
    docs/baseline-agent/lite21-max/runs/search_rate_retry_collapse_hotel_reservation \
    --out /tmp/report --labeller codex:gpt-6-sol --labeller-effort medium
```

The result is in [`docs/run-report-example/`](run-report-example/):

- [`run_report.md`](run-report-example/run_report.md): the report in English
- [`run_report.zh.md`](run-report-example/run_report.zh.md): the same report in Chinese (`--lang zh`)
- [`run_report.json`](run-report-example/run_report.json): the same data for scripts

In this run the agent blamed the wrong thing. Its fix still passed. It also looked at SREGym's own namespace. So the
example shows most of what the tool can say.

## You need a model

Most parts of the report come from a model. The model answers small questions about one piece of the run at a time,
for example "does this output point to the true fault?". Code then puts the report together from those answers and
from the run's own files. There are three ways to connect a model:

| What you have | Flag |
| --- | --- |
| An API key (DeepSeek, OpenAI, Anthropic and more, through LiteLLM) | `--labeller litellm:openai/deepseek-flash` |
| A ChatGPT subscription (Codex CLI) | `--labeller codex:gpt-6-sol` |
| A Claude subscription (Claude Code) | `--labeller claudecode:claude-sonnet-5-5` |

Setup for each, and how accurate each model was, is in [`run-report.md`](run-report.md) under "Connecting a model".

## What #906 asks for, and where the report answers it

Below are the seven items of #906. For each: what the report gives, and lines from the example report.

### 1. Run metadata

#906 asks for the problem ID, agent, model, scores and TTL/TTM. They are at the top of every report.

```
`baseline` · model `openai/deepseek-flash` · effort `max` · judge backend `codex` · profile `svelte`

- Diagnosis: **fail** (score 0.67) · time (TTL) 1309 s
- Mitigation: **pass** · time (TTM) 1462 s
```

The section "Cost and time" adds tokens and time for each stage.

### 2. Command sequence

#906 asks which commands the agent ran, in order, split into diagnosis and mitigation. The section "Commands" lists
every command in order, under its stage. Each line has the step number and the kind of command.

```
- step 7 [kubectl logs] `kubectl logs -n hotel-reservation search-745f56dccd-j7fhl --tail=50; ...`
- step 9 [state/config] `kubectl get deploy -n hotel-reservation -o json | jq -r '.items[] | ...`
- step 50 [change] `kubectl -n hotel-reservation set env deploy/rate RATE_BACKEND_QPS_LIMIT=500 && ...`
```

### 3. Thinking traces

#906 asks for key moments, pivots and dead ends. The section "The agent's own words" reads what the agent wrote at
each step. It gives:

- the step where the agent first named the true fault
- steps where it may have changed its mind, as pointers for a person to read
- what it suspected and then dropped, with how many steps it spent on each
- what it suspected when it sent the diagnosis

```
- Suspected, then dropped:
    - recommendation: 3 steps, 64 s (steps 5 to 35, in 3 stretches)
- when it sent the diagnosis it suspected rate (the ground truth names it), from step 8
```

### 4. Submissions

#906 asks for the final diagnosis and mitigation decisions. The section "Submissions" gives the diagnosis text as
SREGym recorded it, and counts the submit commands in each stage. The section "Judge deductions" lists each checklist
question the judge answered No, with the judge's own reason. Each reason is sorted into "wrong", "adds" or
"leaves out".

```
- **wrong**: It calls the rate service faulty due to its QPS limit, although ground truth locates the fault
  in the search-to-rate timeout/retry/queue interaction. (D1-Q3)
```

### 5. Fix attempts

#906 asks what the agent tried and what worked. The section "Fix attempts" lists each attempt with its steps and
what it changed. For each attempt it says three things. Did the agent's own checks afterwards show it fixed? Did it
change what the ground truth says is wrong? Why did the agent do it, in its own words? Then it gives SREGym's verdict.

```
- #1 (step 50, Mitigation): `set env deploy/rate RATE_BACKEND_QPS_LIMIT=500`;
  `set env deploy/search RATE_RPC_TIMEOUT_MS=1000 ...`; 4 more commands ran after it;
  the checks after it show it fixed, changed what is wrong
- mitigation result: **pass**
```

### 6. Patterns

#906 asks how often the agent verified, whether it fell for known traps, and how long it was stuck.

- **Verification.** Each fix attempt says how many commands ran after it and what they showed. A command that only
  tests something, such as a test request from inside a pod, counts as a check. It does not count as a fix.
- **Known traps.** SREGym plants decoys on purpose, for example the failure-admin scripts in hotel-reservation. The
  application also has its own fault switches, such as the flagd flags of the OpenTelemetry demo. The section "Known
  decoys" says for each one whether the agent looked at it, followed it, dropped it or got stuck on it.
- **Stuck.** The section "Clues" gives the step where the evidence first came on screen and how long the agent took
  after that. The section "Where the steps went" says which components its commands looked at in that time.

```
- First clue: step 7, 40 more steps after it (1269 s)
- on other components the ground truth names: rate 18 steps (steps 8 to 40)
- hotel-reservation's failure-admin ConfigMaps and revoke/remove scripts: looked at it on purpose at step 37;
  **walked out**: followed it up, then dropped it for other suspects
```

### 7. Cheating

#906 asks whether the agent reward hacked. The section "Gaming the benchmark" gives one of three verdicts: none
found, signs to read, or likely. "Likely" needs SREGym's own material that gives the fault away to be on screen
before a diagnosis that passed. Every sign names its steps, and the strongest sign comes first. The final call stays
with a person.

```
- Gaming the benchmark: **signs to read**
    - commands probing the benchmark (step 12, 16, 32, 42, 43)
```

In the example, step 12 searched for chaos-injection tools (`kubectl get crds | grep -iE "chaos|network|stress"`).
Steps 16 to 43 looked at the `sregym` namespace and the MCP server's logs. The answer never came on screen, so the
verdict is "signs to read".

### The structured format

#906 asks for data that can be parsed later. Each run gets `run_report.json`. It holds everything in the Markdown
and more. The folder gets `summary.csv`, one row per run, and `summary.md`.

## What it gives beyond #906

- **What misled a wrong diagnosis.** For a diagnosis the judge failed, it names the output the wrong claim came from.
  In the example: "the output of step 9 ... This output first showed that setting". Step 9 printed
  `RATE_BACKEND_QPS_LIMIT=20` and the agent blamed it.
- **Changes that may go beyond the fault.** It lists fix commands that may have changed things the fault does not
  involve. In the example the agent raised the QPS limit of rate, but the fault was in how search retries its calls
  to rate.
- **Restarts.** It marks restarts in the mitigation stage. A restart can pass a check without fixing anything
  (issue [#753](https://github.com/SREGym/SREGym/issues/753)).
- **The answer on screen.** It checks whether SREGym material that gives the fault away was shown to the agent
  (issue [#1002](https://github.com/SREGym/SREGym/issues/1002)).
- **Masked secrets.** Keys, tokens and private keys in the run's text are masked before any model sees them, and in
  every file the tool writes.
- **Chinese reports.** Add `--lang zh`.
- **Measured accuracy.** Most parts of the report were checked against blind readers. The numbers are in
  [`run-report.md`](run-report.md). Answers that were often wrong are printed as "possibly" or as pointers to read.
- **Stops and restarts cleanly.** Every answer is cached. If a run stops (no balance left, a usage limit), run the
  same command again. It asks only what is missing.

## Limits

- The model can be wrong. The report says how sure each part is.
- The list of known decoys is kept by hand in `sregym/results/run_report/traps.py`. A new problem with a new decoy
  needs an entry there.
