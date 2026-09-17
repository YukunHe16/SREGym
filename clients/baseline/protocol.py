"""Step protocol for the baseline agent.

The baseline agent talks to its model through a fixed JSON protocol. At every
step the model returns exactly one object that either runs one shell command or
submits the diagnosis. This module owns the prompt text, the JSON schemas handed
to the model, the assembly of the submitted text, and the truncation rules that
keep the resent transcript bounded.

The task instruction itself is the one every CLI agent gets
(``clients.codex.driver.build_instruction``), reused verbatim so that runs stay
comparable with the other agents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SUBMISSION_MODES = ("full", "no_mechanism")
DEFAULT_OUTPUT_CHARS = 8000
RECENT_STEPS = 5
OLD_STEP_CHARS = 1500
STDERR_SHARE = 0.25
HEAD_SHARE = 0.75

# Words that explain *why* something fails. A ``no_mechanism`` submission must
# not contain them; the driver re-asks once and then submits with a flag.
MECHANISM_GUARD = re.compile(
    r"\b(because|due to|caused?|causes|causing|root cause|misconfig\w*|mechanism|fix(?:ed|es)?|"
    r"instead of|should be|stale|rotated?|wrong|incorrect|invalid|missing|mismatch\w*|points? to|set to)\b",
    re.IGNORECASE,
)

PROTOCOL_TEXT = """[PROTOCOL]
In this run you interact with the environment only through this JSON protocol. Reply with ONE JSON
object matching the provided schema and nothing else.
- action "command": one non-interactive bash command line. It runs in a container where kubectl is
  configured for the cluster. You receive its stdout, stderr (each truncated) and exit code.
  Do not call the submission endpoint yourself; use action "submit" instead.
- action "submit": {submit_meaning}
- note: one short sentence on what you are doing or concluding (not the diagnosis itself).
{budget_line}
Current stage: {stage}.{submit_only_line}"""

SUBMIT_MEANING = {
    "diagnosis": "end the investigation and submit your diagnosis.",
    "mitigation": (
        "declare that your fix has been applied; the driver then sends the empty mitigation submission that "
        "triggers validation. Any diagnosis text in a mitigation submit is ignored."
    ),
}

MITIGATION_STAGE_TEXT = """[MITIGATION STAGE]
The diagnosis has been submitted. Now fix the problem with commands (kubectl and other tools are allowed to
modify resources in the application namespace). When the application is healthy again, use action "submit"."""

SUBMISSION_TEXT = {
    "full": """[SUBMISSION FORMAT]
When you submit, "diagnosis" is a natural-language description of the issue you found: which component
is at fault, what is wrong with it, why, and the evidence.""",
    "no_mechanism": """[SUBMISSION FORMAT]
When you submit, provide ONLY these three fields:
  1. faulty_component: the single Kubernetes object at fault, written as kind/name.
  2. affected_components: other services or objects that are affected.
  3. symptom: what is observably failing.
Do NOT explain why it fails. Do not name a misconfiguration, mechanism, cause, changed value, or fix.
The submission is assembled from these three fields exactly as given.""",
}

FORMAT_VIOLATION_TEXT = """[NOTICE]
Your previous submission did not follow the submission format: it contained {hits}.
Resubmit with only the component, the affected components and the symptom, and no explanation."""

# Session mode: the driver keeps one growing conversation instead of rebuilding the
# prompt each step. The opening turn is the stateless step-1 prompt; every later
# turn carries only what changed.
STAGE_CHANGE_TEXT = """[STAGE CHANGE]
The diagnosis stage is over. Current stage: {stage}. From now on, action "submit" means: {submit_meaning}"""

UNUSABLE_REPLY_TEXT = """[NOTICE]
Your previous reply could not be used: {error}.
Reply with exactly one JSON object matching the schema and nothing else."""


@dataclass
class StepRecord:
    """One executed (or refused) command and what came back."""

    index: int
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float = 0.0
    timed_out: bool = False
    refused: str | None = None


def build_task_text(app_info: dict) -> str:
    """The instruction every CLI agent gets, byte for byte."""
    from clients.codex.driver import build_instruction

    return build_instruction(app_info)


def budget_line(max_commands: int | None, used: int) -> str:
    if max_commands is None:
        return f"Command budget: there is no fixed command budget in this run. Commands used so far: {used}."
    if max_commands == 0:
        return (
            "Command budget: you cannot run any commands in this run. Submit your best diagnosis now, "
            "based only on the task description above."
        )
    return f"Command budget: you may run at most {max_commands} commands in this run. Used: {used}. Remaining: {max_commands - used}."


def truncate(text: str, limit: int) -> str:
    """Keep the head and the tail of a long output and say how much was cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = int(limit * HEAD_SHARE)
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n[... truncated {cut} chars ...]\n{text[-tail:] if tail else ''}"


def render_step(step: StepRecord, chars: int) -> str:
    stderr_chars = int(chars * STDERR_SHARE)
    stdout_chars = chars - stderr_chars
    lines = [f"Step {step.index} - command: {step.command}"]
    if step.refused:
        lines.append(f"refused: {step.refused}")
    lines.append(f"exit_code: {step.exit_code if step.exit_code is not None else 'none'}")
    if step.timed_out:
        lines.append("timed_out: true")
    lines.append("stdout:")
    lines.append(truncate(step.stdout, stdout_chars) if step.stdout else "(empty)")
    lines.append("stderr:")
    lines.append(truncate(step.stderr, stderr_chars) if step.stderr else "(empty)")
    return "\n".join(lines)


def render_transcript(
    steps: list[StepRecord],
    *,
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    recent: int = RECENT_STEPS,
    old_chars: int = OLD_STEP_CHARS,
) -> str:
    """Recent steps get the full character budget; older ones shrink so the prompt stays bounded."""
    if not steps:
        return ""
    cutoff = max(0, len(steps) - recent)
    rendered = [render_step(step, output_chars if i >= cutoff else old_chars) for i, step in enumerate(steps)]
    return "[TRANSCRIPT]\n" + "\n\n".join(rendered)


def build_step_prompt(
    task_text: str,
    *,
    mode: str,
    max_commands: int | None,
    used: int,
    steps: list[StepRecord],
    submit_only: bool,
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    violation: list[str] | None = None,
    stage: str = "diagnosis",
) -> str:
    if mode not in SUBMISSION_MODES:
        raise ValueError(f"Unknown submission mode: {mode}")
    if stage not in SUBMIT_MEANING:
        raise ValueError(f"Unknown stage: {stage}")
    submit_only_line = ' Only "submit" is accepted at this step.' if submit_only else ""
    parts = [
        "[TASK]\n" + task_text.rstrip(),
        PROTOCOL_TEXT.format(
            submit_meaning=SUBMIT_MEANING[stage],
            budget_line=budget_line(max_commands, used),
            stage=stage,
            submit_only_line=submit_only_line,
        ),
        MITIGATION_STAGE_TEXT if stage == "mitigation" else SUBMISSION_TEXT[mode],
    ]
    transcript = render_transcript(steps, output_chars=output_chars)
    if transcript:
        parts.append(transcript)
    if violation:
        parts.append(FORMAT_VIOLATION_TEXT.format(hits=", ".join(repr(hit) for hit in violation)))
    parts.append(f"[NOW]\nReply with the JSON object for step {len(steps) + 1}.")
    return "\n\n".join(parts) + "\n"


def step_schema(mode: str, submit_only: bool) -> dict:
    """Strict-mode JSON schema: every key required, no extras, optional values nullable."""
    if mode not in SUBMISSION_MODES:
        raise ValueError(f"Unknown submission mode: {mode}")
    properties: dict[str, dict] = {
        "action": {"type": "string", "enum": ["submit"] if submit_only else ["command", "submit"]},
        "note": {"type": "string"},
        "command": {"type": ["string", "null"]},
    }
    if mode == "full":
        properties["diagnosis"] = {"type": ["string", "null"]}
    else:
        properties["faulty_component"] = {"type": ["string", "null"]}
        properties["affected_components"] = {"type": ["array", "null"], "items": {"type": "string"}}
        properties["symptom"] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def validate_step(obj: object, mode: str, submit_only: bool, stage: str = "diagnosis") -> str | None:
    """Return an error message when ``obj`` does not satisfy ``step_schema``; None when it does."""
    if not isinstance(obj, dict):
        return "reply is not a JSON object"
    schema = step_schema(mode, submit_only)
    missing = [key for key in schema["required"] if key not in obj]
    if missing:
        return f"missing keys: {', '.join(missing)}"
    extra = [key for key in obj if key not in schema["properties"]]
    if extra:
        return f"unexpected keys: {', '.join(extra)}"
    action = obj.get("action")
    if action not in schema["properties"]["action"]["enum"]:
        return f"action must be one of {schema['properties']['action']['enum']}, got {action!r}"
    if not isinstance(obj.get("note"), str):
        return "note must be a string"
    if action == "command":
        if not isinstance(obj.get("command"), str) or not obj["command"].strip():
            return "command must be a non-empty string when action is command"
        return None
    if stage == "mitigation":
        return None  # the mitigation submission is always the empty string
    if mode == "full":
        if not isinstance(obj.get("diagnosis"), str) or not obj["diagnosis"].strip():
            return "diagnosis must be a non-empty string when action is submit"
        return None
    if not isinstance(obj.get("faulty_component"), str) or not obj["faulty_component"].strip():
        return "faulty_component must be a non-empty string when action is submit"
    affected = obj.get("affected_components")
    if affected is not None and not (isinstance(affected, list) and all(isinstance(item, str) for item in affected)):
        return "affected_components must be a list of strings or null"
    if not isinstance(obj.get("symptom"), str) or not obj["symptom"].strip():
        return "symptom must be a non-empty string when action is submit"
    return None


def mechanism_guard_hits(parsed: dict, mode: str) -> list[str]:
    """Mechanism words found in a ``no_mechanism`` submission; always empty for ``full``."""
    if mode != "no_mechanism":
        return []
    fields = [parsed.get("faulty_component") or "", parsed.get("symptom") or ""]
    fields.extend(parsed.get("affected_components") or [])
    hits: list[str] = []
    for text in fields:
        hits.extend(match.group(0) for match in MECHANISM_GUARD.finditer(text))
    return sorted(set(hit.lower() for hit in hits))


def compose_submission(parsed: dict, mode: str, stage: str = "diagnosis") -> str:
    """The text that goes to the conductor."""
    if stage == "mitigation":
        return ""
    if mode == "full":
        return parsed["diagnosis"].strip()
    affected = [item.strip() for item in (parsed.get("affected_components") or []) if item.strip()]
    affected_text = ", ".join(affected) if affected else "none identified"
    return (
        f"Faulty component: {parsed['faulty_component'].strip()}. "
        f"Affected components: {affected_text}. "
        f"Observed symptom: {parsed['symptom'].strip()}."
    )


def stage_change_text(stage: str) -> str:
    """The turn that moves a session from diagnosis into ``stage``."""
    if stage not in SUBMIT_MEANING:
        raise ValueError(f"Unknown stage: {stage}")
    text = STAGE_CHANGE_TEXT.format(stage=stage, submit_meaning=SUBMIT_MEANING[stage])
    if stage == "mitigation":
        text += "\n\n" + MITIGATION_STAGE_TEXT
    return text


def build_session_turn(
    step: StepRecord | None,
    *,
    max_commands: int | None,
    used: int,
    next_step: int,
    submit_only: bool,
    output_chars: int = DEFAULT_OUTPUT_CHARS,
    violation: list[str] | None = None,
    stage_change: str | None = None,
    unusable_reply: str | None = None,
) -> str:
    """One follow-up user turn: the last command's result, notices, the budget, and the step to answer."""
    parts: list[str] = []
    if step is not None:
        parts.append("[RESULT]\n" + render_step(step, output_chars))
    if stage_change:
        parts.append(stage_change)
    if unusable_reply:
        parts.append(UNUSABLE_REPLY_TEXT.format(error=unusable_reply))
    if violation:
        parts.append(FORMAT_VIOLATION_TEXT.format(hits=", ".join(repr(hit) for hit in violation)))
    status = budget_line(max_commands, used)
    if submit_only:
        status += ' Only "submit" is accepted at this step.'
    parts.append("[STATUS]\n" + status)
    parts.append(f"[NOW]\nReply with the JSON object for step {next_step}.")
    return "\n\n".join(parts) + "\n"
