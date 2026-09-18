"""The mini-swe-agent recipe for the baseline agent (the default protocol).

Everything the model sees is plain text: a system prompt that asks for exactly ONE ```bash
code block per reply, the task instruction the CLI agents get, and one observation per
command (return code plus output, long outputs shortened to their head and tail). Each
command runs in a fresh subshell. The model finishes a stage with a command whose output
starts with the line ``COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT``; what follows that line is the
submission. Templates and rules follow mini-swe-agent v1.17.5 (``agents/default.py`` and
``config/mini.yaml``), adapted only where SREGym needs a submission text and two stages.
"""

from __future__ import annotations

import re

from clients.baseline.protocol import MECHANISM_GUARD, SUBMISSION_MODES, build_task_text

MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
SUBMIT_MODES = ("auto", "curl", "marker")
# curl: the model submits exactly as the task instruction says (POST /submit itself) and the driver only
# watches the conductor's stage; this is the CLI agents' condition and the default for unrestricted arms.
# marker: the driver owns the submission through the marker command; needed by budget and no_mechanism arms.
CURL_INSTANCE_NOTE = """

Note: every command is executed in a new subshell; directory or environment variable changes do not persist."""
ACTION_REGEX = re.compile(r"```bash\s*\n(.*?)\n```", re.DOTALL)
OUTPUT_LIMIT = 10_000  # mini-swe-agent's observation limit: head and tail of 5,000 characters each
MAX_CONSECUTIVE_FORMAT_ERRORS = 3

SYSTEM_TEMPLATE = """You are a helpful assistant that can interact with a computer.
Your response must contain exactly ONE bash code block with ONE command (or commands connected with && or ||).
Include a THOUGHT section before your command where you explain your reasoning process.
Format your response as shown in <format_example>.

<format_example>
Your reasoning and analysis here. Explain why you want to perform the action.

```bash
your_command_here
```
</format_example>

Failure to follow these rules will cause your response to be rejected."""

SUBMIT_DIAGNOSIS_EXAMPLE = {
    "full": f"""```bash
cat <<'EOF'
{MARKER}
Your diagnosis: which component is at fault, what is wrong with it, why, and the evidence.
EOF
```""",
    "no_mechanism": f"""```bash
cat <<'EOF'
{MARKER}
faulty_component: <the single Kubernetes object at fault, as kind/name>
affected_components: <other services or objects that are affected, comma-separated>
symptom: <what is observably failing>
EOF
```""",
}

SUBMISSION_RULES = {
    "full": "",
    "no_mechanism": (
        "\nYour diagnosis consists of exactly those three lines. Do NOT explain why it fails: do not name a "
        "misconfiguration, mechanism, cause, changed value, or fix.\n"
    ),
}

WORKFLOW_MODES = ("none", "general")
# The designed prompt, kept deliberately domain-general: it says how to investigate, never where to look.
# Nothing here names a kind of Kubernetes object, a class of fault, or anything the judge scores, so the
# same text would be right on a benchmark whose faults were distributed completely differently.
WORKFLOW_TEXT = """

## How to work

1. Before each command, say what you currently think is wrong and what this command would tell you.
   A command whose result cannot change your mind is not worth running.
2. Read the output you asked for before asking for more, and say what it rules in and what it rules out.
3. If two commands in a row tell you nothing new about the current idea, drop it and try another one.
4. Survey the whole system before going deep into any one part of it.
5. Stop as soon as your evidence answers the task. Evidence gathered after that point cannot improve your
   answer, and the stage ends whether or not you have submitted."""

MITIGATION_WORKFLOW_TEXT = """
Change one thing at a time and check its effect before making the next change."""

INSTANCE_TEMPLATE = """{task}

## Important Rules

1. Every response must contain exactly one action
2. The action must be enclosed in triple backticks
3. Directory or environment variable changes are not persistent. Every action is executed in a new subshell.
4. Do not call the submission endpoint yourself. Submit with the command described below; the driver forwards it.
{budget_rule}
## How to submit

Current stage: diagnosis. To submit your diagnosis, run a command whose output starts with the line `{marker}`,
followed by the diagnosis, for example:

{submit_example}
{submission_rules}
Do not combine it with any other command. <important>After this command, you cannot continue investigating.</important>"""

BUDGET_RULE = """5. You may run at most {max_commands} commands in this stage; the submission command does not count.
"""
ZERO_BUDGET_RULE = """5. You cannot run any commands in this stage. Your only action is the submission command below, based on the task description.
"""

MITIGATION_TEMPLATE = """The diagnosis has been submitted. Current stage: mitigation.
Now fix the problem with commands (kubectl and other tools are allowed to modify resources in the application namespace).
When the application is healthy again, finish by issuing the following command: `echo {marker}`.
Do not combine it with any other command. <important>After this command, you cannot continue working on this task.</important>{budget_line}"""

ACTION_OBSERVATION_TEMPLATE = """<returncode>{returncode}</returncode>
{body}"""

OUTPUT_TOO_LONG_WARNING = """<warning>
The output of your last command was too long.
Please try a different command that produces less output.
If you're looking at a file you can try use head, tail or sed to view a smaller number of lines selectively.
If you're using grep or find and it produced too much output, you can use a more selective search pattern.
If you really need to see something from the full command's output, you can redirect output to a file and then search in that file.
</warning>"""

FORMAT_ERROR_TEMPLATE = """Please always provide EXACTLY ONE action in triple backticks, found {n} actions.
If you want to end the stage, issue the submission command described in the task (its output must start with `{marker}`)
without any other command.
Else, please format your response exactly as follows:
<response_example>
Here are some thoughts about why you want to perform the action.

```bash
<action>
```
</response_example>
Note: In rare cases, if you need to reference a similar format in your command, you might have
to proceed in two steps, first writing TRIPLEBACKTICKSBASH, then replacing them with ```bash."""

TIMEOUT_TEMPLATE = """The last command <command>{action}</command> timed out and has been killed.
The output of the command was:
{body}
Please try another command and make sure to avoid those requiring interactive input."""

BUDGET_EXHAUSTED_TEMPLATE = """You have used the {max_commands} commands allowed in this stage. The only action left is the submission command
(its output must start with `{marker}`). Submit your best answer now."""

COUNTDOWN_COMMANDS = 15  # the last stretch before the cap, where the notice turns urgent
COUNTDOWN_SECONDS = 300  # ... and the last stretch before the stage deadline

BUDGET_LINE_TEMPLATE = "\n<budget>command {used} of {cap}, about {minutes} minutes left in this stage</budget>"

LIMIT_NOTICE_TEMPLATE = """
<IMPORTANT>
You have {left} left in this stage. When that runs out the stage ends, and if you have not submitted by then,
nothing is recorded for it. Stop pulling on whatever thread you are on and come back to the big picture:
submit your best answer now. {how}
</IMPORTANT>"""

WRAP_UP_TEMPLATE = """You have reached {reason} for this stage. Stop investigating and submit your best answer now,
based on what you already know. {how}"""

WRAP_UP_HOW = {
    "curl": "Submit it to the conductor exactly as the task instruction describes.",
    "marker": f"The only action left is the submission command (its output must start with `{MARKER}`).",
}

REJECTED_COMMAND_OBSERVATION = "<returncode>126</returncode>\n<output>\nrejected: {reason}\n</output>"

GUARD_REJECTION = (
    "rejected: the submission explained a mechanism (it contained {hits}). Submit again with only the three lines "
    "(faulty_component, affected_components, symptom) and no explanation."
)


def system_text() -> str:
    return SYSTEM_TEMPLATE


def budget_rule(max_commands: int | None) -> str:
    if max_commands is None:
        return ""
    if max_commands == 0:
        return ZERO_BUDGET_RULE
    return BUDGET_RULE.format(max_commands=max_commands)


def resolve_submit_mode(value: str, *, mode: str, max_commands: int | None) -> str:
    """``auto`` is curl for an unrestricted full-text arm and marker otherwise; curl cannot serve the other arms."""
    if value not in SUBMIT_MODES:
        raise ValueError(f"BASELINE_SUBMIT must be one of {SUBMIT_MODES}")
    needs_driver = mode != "full" or max_commands is not None
    if value == "auto":
        return "marker" if needs_driver else "curl"
    if value == "curl" and needs_driver:
        raise ValueError("BASELINE_SUBMIT=curl needs BASELINE_SUBMISSION_MODE=full and an unlimited budget")
    return value


def workflow_text(workflow: str) -> str:
    if workflow not in WORKFLOW_MODES:
        raise ValueError(f"Unknown workflow: {workflow}")
    return WORKFLOW_TEXT if workflow == "general" else ""


def instance_text(
    app_info: dict, *, mode: str, max_commands: int | None, submit_mode: str = "marker", workflow: str = "none"
) -> str:
    """The first user turn: SREGym's task instruction (verbatim), the protocol's own rules, and — only when
    the arm asks for it — the general workflow."""
    if mode not in SUBMISSION_MODES:
        raise ValueError(f"Unknown submission mode: {mode}")
    if submit_mode == "curl":
        return build_task_text(app_info).rstrip() + CURL_INSTANCE_NOTE + workflow_text(workflow)
    return (
        INSTANCE_TEMPLATE.format(
            task=build_task_text(app_info).rstrip(),
            budget_rule=budget_rule(max_commands),
            marker=MARKER,
            submit_example=SUBMIT_DIAGNOSIS_EXAMPLE[mode],
            submission_rules=SUBMISSION_RULES[mode],
        )
        + workflow_text(workflow)
    )


def mitigation_text(max_commands: int | None, workflow: str = "none") -> str:
    line = ""
    if max_commands is not None:
        line = (
            "\nYou cannot run any commands in this stage; your only action is the finishing command."
            if max_commands == 0
            else f"\nYou may run at most {max_commands} commands in this stage; the finishing command does not count."
        )
    extra = MITIGATION_WORKFLOW_TEXT if workflow_text(workflow) else ""
    return MITIGATION_TEMPLATE.format(marker=MARKER, budget_line=line) + extra


def parse_action(content: str) -> tuple[str | None, int]:
    """(the single action, number of code blocks found); the action is None unless exactly one block is present."""
    actions = ACTION_REGEX.findall(content or "")
    if len(actions) == 1:
        return actions[0].strip(), 1
    return None, len(actions)


def format_error_text(n_actions: int) -> str:
    return FORMAT_ERROR_TEMPLATE.format(n=n_actions, marker=MARKER)


def _output_body(output: str) -> str:
    if len(output) < OUTPUT_LIMIT:
        return f"<output>\n{output}\n</output>"
    elided = len(output) - OUTPUT_LIMIT
    return (
        f"{OUTPUT_TOO_LONG_WARNING}\n<output_head>\n{output[:5000]}\n</output_head>\n<elided_chars>\n{elided} characters elided\n"
        f"</elided_chars>\n<output_tail>\n{output[-5000:]}\n</output_tail>"
    )


def observation_text(returncode: int, output: str) -> str:
    return ACTION_OBSERVATION_TEMPLATE.format(returncode=returncode, body=_output_body(output))


def timeout_text(action: str, output: str) -> str:
    return TIMEOUT_TEMPLATE.format(action=action, body=_output_body(output))


def budget_exhausted_text(max_commands: int) -> str:
    return BUDGET_EXHAUSTED_TEMPLATE.format(max_commands=max_commands, marker=MARKER)


def budget_notice(*, used: int, cap: int, seconds_left: float, submit_mode: str) -> str:
    """What every observation of an unrestricted arm carries: how much of the stage is left.

    It states the budget and nothing else — never a hint about where the fault might be — so an arm
    that paces itself is still being scored on its own investigation. mini-swe-agent's long-horizon
    config does the same thing for the last stretch; here the count runs from the first command,
    because a stage of open-ended diagnosis has no finish line the model can see for itself.
    """
    commands_left = max(cap - used, 0)
    minutes = max(int(seconds_left // 60), 0)
    urgent = []
    if commands_left <= COUNTDOWN_COMMANDS:
        urgent.append(f"{commands_left} command{'' if commands_left == 1 else 's'}")
    if seconds_left <= COUNTDOWN_SECONDS:
        urgent.append(f"about {minutes} minute{'' if minutes == 1 else 's'}")
    if urgent:
        return LIMIT_NOTICE_TEMPLATE.format(left=" and ".join(urgent), how=WRAP_UP_HOW[submit_mode])
    return BUDGET_LINE_TEMPLATE.format(used=used, cap=cap, minutes=minutes)


def wrap_up_text(reason: str, submit_mode: str) -> str:
    """The one notice an unrestricted arm gets when it runs into the driver's limits."""
    if submit_mode not in WRAP_UP_HOW:
        raise ValueError(f"unknown submit mode: {submit_mode}")
    return WRAP_UP_TEMPLATE.format(reason=reason, how=WRAP_UP_HOW[submit_mode])


def finished(output: str, returncode: int) -> str | None:
    """The submission when the command output starts with the marker (and the command succeeded), else None."""
    lines = (output or "").lstrip().splitlines(keepends=True)
    if not lines or lines[0].strip() != MARKER:
        return None
    if returncode != 0:
        return None  # let the model see the error and retry, as mini-swe-agent does
    return "".join(lines[1:]).strip()


def parse_three_fields(text: str) -> tuple[dict | None, str | None]:
    """The no_mechanism submission: three ``key: value`` lines."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    missing = [k for k in ("faulty_component", "affected_components", "symptom") if not fields.get(k)]
    if missing:
        return None, (
            "the submission must contain the lines faulty_component, affected_components and symptom "
            f"(missing: {', '.join(missing)})"
        )
    return fields, None


def mechanism_guard_hits(fields: dict) -> list[str]:
    hits: list[str] = []
    for text in (fields.get("faulty_component", ""), fields.get("affected_components", ""), fields.get("symptom", "")):
        hits.extend(match.group(0) for match in MECHANISM_GUARD.finditer(text))
    return sorted(set(hit.lower() for hit in hits))


def compose_three_fields(fields: dict) -> str:
    affected = [item.strip() for item in fields.get("affected_components", "").split(",") if item.strip()]
    affected_text = ", ".join(affected) if affected and affected != ["none"] else "none identified"
    return (
        f"Faulty component: {fields['faulty_component']}. Affected components: {affected_text}. "
        f"Observed symptom: {fields['symptom']}."
    )
