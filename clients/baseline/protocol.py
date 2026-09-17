"""Prompt pieces and submission rules for the tool-calling baseline agent.

The task instruction is the one every CLI agent gets (``clients.codex.driver.build_instruction``),
reused verbatim so that runs stay comparable. Everything else here is short: a system line about
working through tools, a budget line, the stage-change turn, the notices the driver sends when
it has to intervene, and the shape of the driver-owned ``submit`` tool.
"""

from __future__ import annotations

import re

from clients.baseline.tools import ToolSpec

SUBMISSION_MODES = ("full", "no_mechanism")
STAGES = ("diagnosis", "mitigation")
DEFAULT_OUTPUT_CHARS = 8000
HEAD_SHARE = 0.75

# Words that explain *why* something fails. A ``no_mechanism`` submission must not contain them;
# the driver rejects the submit call once and then accepts the next one with a flag.
MECHANISM_GUARD = re.compile(
    r"\b(because|due to|caused?|causes|causing|root cause|misconfig\w*|mechanism|fix(?:ed|es)?|"
    r"instead of|should be|stale|rotated?|wrong|incorrect|invalid|missing|mismatch\w*|points? to|set to)\b",
    re.IGNORECASE,
)

SYSTEM_TEXT = """You are an SRE agent. You work only through the tools provided: investigate with them, change
things with them when the task calls for it, and call the submit tool when you are done. Reply with tool
calls; a reply without a tool call does not advance the task. Do not call the submission endpoint with a
shell command; use the submit tool instead.
{submission_rules}"""

SUBMISSION_RULES = {
    "full": (
        'Diagnosis submissions: the "diagnosis" argument is a natural-language description of the issue you found: '
        "which component is at fault, what is wrong with it, why, and the evidence."
    ),
    "no_mechanism": (
        "Diagnosis submissions take exactly three arguments: faulty_component (the single Kubernetes object at "
        "fault, written as kind/name), affected_components (other services or objects that are affected) and "
        "symptom (what is observably failing). Do NOT explain why it fails: do not name a misconfiguration, "
        "mechanism, cause, changed value, or fix. The submission is assembled from these arguments exactly as given."
    ),
}

STAGE_HINT = {
    "diagnosis": "Call submit when you have your diagnosis.",
    "mitigation": (
        "Fix the problem with the tools (modifying resources in the application namespace is allowed). When the "
        "application is healthy again, call submit; the driver then sends the empty mitigation submission that "
        "triggers validation."
    ),
}

STAGE_CHANGE_TEXT = """[STAGE CHANGE]
The diagnosis has been submitted. Current stage: mitigation. {hint}
{budget_line}"""

FORCED_SUBMIT_TEXT = """[NOTICE]
You have reached the limit for this stage ({reason}). Only the submit tool is available now: submit your best
answer with it."""

NUDGE_TEXT = """[NOTICE]
Your last reply contained no tool call. Continue with a tool call, or call submit when you are done."""

UNUSABLE_REPLY_TEXT = """[NOTICE]
Your previous reply could not be used: {error}.
Reply with a tool call."""

GUARD_REJECTION_TEXT = (
    "rejected: the submission explained a mechanism (it contained {hits}). Call submit again with only the "
    "component, the affected components and the symptom, and no explanation."
)

FALLBACK_DIAGNOSIS = "No diagnosis could be produced."


def build_task_text(app_info: dict) -> str:
    """The instruction every CLI agent gets, byte for byte."""
    from clients.codex.driver import build_instruction

    return build_instruction(app_info)


def system_text(mode: str) -> str:
    if mode not in SUBMISSION_MODES:
        raise ValueError(f"Unknown submission mode: {mode}")
    return SYSTEM_TEXT.format(submission_rules=SUBMISSION_RULES[mode])


def budget_line(max_calls: int | None, used: int) -> str:
    if max_calls is None:
        return f"Tool-call budget: there is no fixed tool-call budget in this stage. Tool calls used so far: {used}."
    if max_calls == 0:
        return (
            "Tool-call budget: you cannot call any tool other than submit in this stage. Submit your best answer "
            "now, based only on the task description."
        )
    return (
        f"Tool-call budget: you may make at most {max_calls} tool calls in this stage (submit does not count). "
        f"Used: {used}. Remaining: {max_calls - used}."
    )


def opening_turn(task_text: str, *, stage: str, max_calls: int | None) -> str:
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    return (
        "[TASK]\n"
        + task_text.rstrip()
        + "\n\n[PROTOCOL]\n"
        + budget_line(max_calls, 0)
        + f"\nCurrent stage: {stage}. {STAGE_HINT[stage]}\n"
    )


def stage_change_turn(max_calls: int | None) -> str:
    return STAGE_CHANGE_TEXT.format(hint=STAGE_HINT["mitigation"], budget_line=budget_line(max_calls, 0)) + "\n"


def forced_submit_turn(reason: str) -> str:
    return FORCED_SUBMIT_TEXT.format(reason=reason) + "\n"


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


def submit_tool_spec(mode: str, stage: str) -> ToolSpec:
    """The driver-owned submit tool; its arguments depend on the stage and the submission mode."""
    if mode not in SUBMISSION_MODES:
        raise ValueError(f"Unknown submission mode: {mode}")
    if stage == "mitigation":
        return ToolSpec(
            "submit",
            "Declare that your fix has been applied. The driver then sends the empty mitigation submission that "
            "triggers validation.",
            {
                "type": "object",
                "properties": {"note": {"type": "string", "description": "Optional one-line summary of the fix."}},
            },
            "submit",
        )
    if stage != "diagnosis":
        raise ValueError(f"Unknown stage: {stage}")
    if mode == "full":
        return ToolSpec(
            "submit",
            "End the investigation and submit your diagnosis.",
            {
                "type": "object",
                "properties": {
                    "diagnosis": {
                        "type": "string",
                        "description": "Natural-language diagnosis: faulty component, what is wrong, why, evidence.",
                    }
                },
                "required": ["diagnosis"],
            },
            "submit",
        )
    return ToolSpec(
        "submit",
        "End the investigation and submit your diagnosis as three fields. Do not explain why it fails.",
        {
            "type": "object",
            "properties": {
                "faulty_component": {
                    "type": "string",
                    "description": "The single Kubernetes object at fault, as kind/name.",
                },
                "affected_components": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Other services or objects that are affected.",
                },
                "symptom": {"type": "string", "description": "What is observably failing."},
            },
            "required": ["faulty_component", "affected_components", "symptom"],
        },
        "submit",
    )


def validate_submit(args: object, mode: str, stage: str) -> str | None:
    """Return an error message when the submit arguments are unusable; None when they are fine."""
    if not isinstance(args, dict):
        return "submit arguments are not an object"
    if stage == "mitigation":
        return None  # the mitigation submission is always the empty string
    if mode == "full":
        if not isinstance(args.get("diagnosis"), str) or not args["diagnosis"].strip():
            return "diagnosis must be a non-empty string"
        return None
    if not isinstance(args.get("faulty_component"), str) or not args["faulty_component"].strip():
        return "faulty_component must be a non-empty string"
    affected = args.get("affected_components")
    if affected is not None and not (isinstance(affected, list) and all(isinstance(item, str) for item in affected)):
        return "affected_components must be a list of strings"
    if not isinstance(args.get("symptom"), str) or not args["symptom"].strip():
        return "symptom must be a non-empty string"
    return None


def mechanism_guard_hits(args: dict, mode: str) -> list[str]:
    """Mechanism words found in a ``no_mechanism`` submission; always empty for ``full``."""
    if mode != "no_mechanism":
        return []
    fields = [args.get("faulty_component") or "", args.get("symptom") or ""]
    fields.extend(args.get("affected_components") or [])
    hits: list[str] = []
    for text in fields:
        hits.extend(match.group(0) for match in MECHANISM_GUARD.finditer(str(text)))
    return sorted(set(hit.lower() for hit in hits))


def compose_submission(args: dict, mode: str, stage: str) -> str:
    """The text that goes to the conductor."""
    if stage == "mitigation":
        return ""
    if mode == "full":
        return args["diagnosis"].strip()
    affected = [item.strip() for item in (args.get("affected_components") or []) if item.strip()]
    affected_text = ", ".join(affected) if affected else "none identified"
    return (
        f"Faulty component: {args['faulty_component'].strip()}. "
        f"Affected components: {affected_text}. "
        f"Observed symptom: {args['symptom'].strip()}."
    )
