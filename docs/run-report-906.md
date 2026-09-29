# Run report: what it does for issue #906

Issue [#906](https://github.com/SREGym/SREGym/issues/906) asks for a tool that reads finished runs and tells you what
the agent did. Today people hand each `driver.log` to an AI agent and ask questions. That does not scale.

`sregym.results.run_report` is that tool. You point it at a results folder. For each run it writes a report for
people (Markdown) and the same data for scripts (JSON). For the whole folder it writes one table.

It only reads files. It does not touch a cluster, run an agent again or change a score.

## An example you can run

This repository ships one run of the baseline agent on each SREGym-Lite problem, in
[`docs/baseline-agent/lite21-qwen/`](baseline-agent/lite21-qwen/). The report below was made from the run on
`secret_rotation_stale_env_credentials_astronomy_shop` with this command:

```bash
python -m sregym.results.run_report \
    docs/baseline-agent/lite21-qwen/runs/secret_rotation_stale_env_credentials_astronomy_shop \
    --out /tmp/report --labeller codex:gpt-6-sol --labeller-effort medium
```

The result is in [`docs/run-report-example/`](run-report-example/):

- [`run_report.md`](run-report-example/run_report.md): the report in English
- [`run_report.zh.md`](run-report-example/run_report.zh.md): the same report in Chinese (`--lang zh`)
- [`run_report.json`](run-report-example/run_report.json): the same data for scripts

In this run the agent blamed the wrong thing: PostgreSQL's password setup. Its fix still passed. It also read
SREGym's own code inside its container. So the example shows most of what the tool can say.

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
`baseline` · model `openai/qwen3.8-27b-mtp` · effort `not recorded` · judge backend `codex` · profile `svelte`

- Diagnosis: **fail** (score 0.33) · time (TTL) 596 s
- Mitigation: **pass** · time (TTM) 764 s
```

The section "Cost and time" adds tokens and time for each stage.

### 2. Command sequence

#906 asks which commands the agent ran, in order, split into diagnosis and mitigation. The section "Commands" lists
every command in order, under its stage. Each line has the step number and the kind of command.

```
- step 21 [kubectl logs] `export KUBECONFIG=/tmp/kubeconfig && kubectl logs -n astronomy-shop frontend-6859c775df-84hj6 2>&1 | tail -20`
- step 56 [change] `export KUBECONFIG=/tmp/kubeconfig && kubectl rollout restart deployment product-catalog -n astronomy-shop 2>&1`
- step 71 [submit] `curl -s -X POST http://host.docker.internal:8000/submit -H "Content-Type: application/json" -d ...`
```

### 3. Thinking traces

#906 asks for key moments, pivots and dead ends. The section "The agent's own words" reads what the agent wrote at
each step. It gives:

- the step where the agent first named the true fault
- steps where it may have changed its mind, as pointers for a person to read
- what it suspected and then dropped, with how many steps it spent on each
- what it suspected when it sent the diagnosis

```
- Key moments: evidence on screen at step 21; the true fault named 19 steps, 124 s later, at step 40;
  diagnosis submitted at step 71
- Suspected, then dropped:
    - probably the longest: accounting: 4 steps, 18 s (steps 18 to 43, in 2 stretches)
- when it sent the diagnosis it suspected postgresql (the ground truth names it; the faulty component it gives
  is product-catalog), from step 70
```

### 4. Submissions

#906 asks for the final diagnosis and mitigation decisions. The section "Submissions" gives the diagnosis text as
SREGym recorded it, and counts the submit commands in each stage. The section "Judge deductions" lists each checklist
question the judge answered No, with the judge's own reason. Each reason is sorted into "wrong", "adds" or "omits".

```
- **wrong**: It attributes the root cause partly to PostgreSQL's pg_authid hash rather than the stale runtime
  credential in the active product-catalog pod. (D1-Q3)
```

### 5. Fix attempts

#906 asks what the agent tried and what worked. The section "Fix attempts" lists each attempt with its steps and
what it changed. For each attempt it says three things. Did the agent's own checks afterwards show it fixed? Did it
change what the ground truth says is wrong? Why did the agent do it, in its own words? Then it gives SREGym's verdict.

```
- #1 (step 56, Diagnosis): `rollout restart deployment product-catalog`; 9 more commands ran after it;
  the checks after it are unclear, changed what is wrong (restarted the faulty component product-catalog)
    - why (in its own words, step 56): “The fix is simple: restart the product-catalog so it gets a fresh
      connection to the now-ready postgres.”
- mitigation result: **pass**
```

### 6. Patterns

#906 asks how often the agent verified, whether it fell for known traps, and how long it was stuck.

- **Verification.** Each fix attempt says how many commands ran after it and what they showed. A command that only
  tests something, such as a test request from inside a pod, counts as a check. It does not count as a fix.
- **Known traps.** SREGym plants decoys on purpose, for example the failure-admin scripts in hotel-reservation. The
  application also has its own fault switches, such as the flagd flags of the OpenTelemetry demo. The section "Known
  decoys" says for each one whether the agent looked at it, followed it, dropped it or got stuck on it. The example
  run met none of them, so its report has no such section.
- **Stuck.** The section "Clues" gives the step where the evidence first came on screen and how long the agent took
  after that. The section "Where the steps went" says which components its commands looked at in that time.

```
- First clue: step 21, 50 more steps after it (492 s)
- on other components the ground truth names: postgresql 5 steps (steps 35 to 39)
```

### 7. Cheating

#906 asks whether the agent reward hacked. The section "Gaming the benchmark" gives one of three verdicts: none
found, signs to read, or likely. "Likely" needs SREGym's own material that gives the fault away to be on screen
before a diagnosis that passed. Every sign names its steps, and the strongest sign comes first. The final call stays
with a person.

```
- Gaming the benchmark: **signs to read**
    - reads SREGym's code inside the agent container (step 8, 9, 10, 11, 12)
```

In the example, steps 7 to 12 listed `/opt` and `/opt/sregym` and read `sregym/service/kubectl.py`. That is the part of SREGym
that ships inside the agent's image. The answer never came on screen, so the verdict is "signs to read".

### The structured format

#906 asks for data that can be parsed later. Each run gets `run_report.json`. It holds everything in the Markdown
and more. The folder gets `summary.csv`, one row per run, and `summary.md`.

## What it gives beyond #906

- **What misled a wrong diagnosis.** For a diagnosis the judge failed, it names the output the wrong claim came from.
  In the example it names the output of step 32, a PostgreSQL log line saying the failed connection matched a
  scram-sha-256 rule. The agent went on to blame PostgreSQL's password hash.
- **Changes that may go beyond the fault.** It lists fix commands that may have changed things the fault does not
  involve.
- **Restarts.** It marks restarts of the faulty component. A restart can pass a check without fixing anything
  (issue [#753](https://github.com/SREGym/SREGym/issues/753)). In the example the restart of product-catalog counts
  as aimed at the fault: the fault was the old connection string the pod read when it started.
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
