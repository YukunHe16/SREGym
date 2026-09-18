"""
Baseline agent driver for SREGym.

Default protocol ``mini`` is the mini-swe-agent recipe: the model gets the same task
instruction as the CLI agents, replies with exactly one ```bash block per turn, each command
runs in a fresh subshell, the observation is the return code plus the output, and a stage
ends when a command prints the marker line followed by the submission. Limits end the stage
without a submission, like a CLI agent that stops; budget arms get one notice and may then
only submit. Protocol ``tools`` (BASELINE_PROTOCOL=tools) is the tool-calling variant: local
tools (bash, read_file, write_file), optional MCP tools, a driver-owned submit tool, and a
Stratus-style forced last turn at the limits.

Knobs (environment):

    BASELINE_MAX_COMMANDS      tool calls per stage: 0, a positive integer, or "unlimited"
    BASELINE_SUBMISSION_MODE   "full" or "no_mechanism" (three fields, no explanation of why)
    BASELINE_TOOLS             comma list, default bash,read_file,write_file; add mcp:kubectl,mcp:prometheus,mcp:jaeger,mcp:loki
    BASELINE_HARD_CAP / BASELINE_DEADLINE_S / BASELINE_COMMAND_TIMEOUT / BASELINE_OUTPUT_CHARS

The conversation is one growing message list (the model's replies and, when the provider
returns it, its reasoning go back each step). Only anonymous artifact ids reach disk.
"""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

# Add SREGym root to path
sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from logger import init_logger  # noqa: E402

init_logger()

from clients.baseline import mini, protocol  # noqa: E402
from clients.baseline.backends import ApiBackend, Reply, parse_arguments  # noqa: E402
from clients.baseline.tools import (  # noqa: E402,F401  (CommandResult / run_command re-exported for tests)
    CommandResult,
    McpToolbox,
    Toolbox,
    ToolSpec,
    parse_tool_selection,
    run_command,
)
from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from clients.harness.token_usage import aggregate_usage  # noqa: E402

logger = logging.getLogger("all.baseline.driver")

# ---------------------------------------------------------------------------
# Config from environment (module level so tests can patch it)
# ---------------------------------------------------------------------------

API_HOSTNAME = os.getenv("API_HOSTNAME", "localhost")
API_PORT = os.getenv("API_PORT", "8000")
CONDUCTOR_URL = f"http://{API_HOSTNAME}:{API_PORT}"

AGENT_LOGS_DIR = os.environ.get("AGENT_LOGS_DIR", "./logs/baseline")
MODEL = os.environ.get("AGENT_MODEL_ID", "openai/glm-5.3")
REASONING_EFFORT = os.environ.get("AGENT_REASONING_EFFORT")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")

PROTOCOL = os.environ.get("BASELINE_PROTOCOL", "mini")  # mini: one bash block per reply; tools: function calling
SUBMIT_RAW = os.environ.get(
    "BASELINE_SUBMIT", "auto"
)  # mini protocol: curl (as the task says) or marker (driver-owned)
PROTOCOLS = ("mini", "tools")
MAX_COMMANDS_RAW = os.environ.get("BASELINE_MAX_COMMANDS", "unlimited")
SUBMISSION_MODE = os.environ.get("BASELINE_SUBMISSION_MODE", "full")
TOOLS_RAW = os.environ.get("BASELINE_TOOLS")
HARD_CAP = int(os.environ.get("BASELINE_HARD_CAP", "80"))
# Replies an unrestricted arm gets after the wrap-up notice to put its submission through.
WRAP_UP_CALLS = int(os.environ.get("BASELINE_WRAP_UP_CALLS", "3"))
COMMAND_TIMEOUT = int(os.environ.get("BASELINE_COMMAND_TIMEOUT", "60"))
OUTPUT_CHARS = int(os.environ.get("BASELINE_OUTPUT_CHARS", "8000"))
DEADLINE_S = float(os.environ.get("BASELINE_DEADLINE_S", "1500"))
RETRY_WAIT_S = float(os.environ.get("BASELINE_RETRY_WAIT_S", "30"))
SESSION_REASONING = os.environ.get("BASELINE_SESSION_REASONING", "1") != "0"
MAX_NUDGES = 2  # replies without a tool call before the driver forces a submission

STORED_STREAM_CHARS = 256 * 1024
SUBMIT_ENDPOINT = re.compile(r"/submit(?:_mcp)?\b")

EXIT_OK = 0
EXIT_INFRA = 1
EXIT_MODEL_FAILURE = 2
EXIT_EXTERNAL_SUBMISSION = 3
EXIT_NO_SUBMISSION = 4  # the stage ended without a submission (limits, repeated format errors)


def parse_budget(value: str) -> int | None:
    """``None`` means unlimited (bounded only by the hard cap and the deadline)."""
    text = str(value).strip().lower()
    if text in {"unlimited", "none", "inf", ""}:
        return None
    budget = int(text)
    if budget < 0:
        raise ValueError("BASELINE_MAX_COMMANDS must be 0, a positive integer, or unlimited")
    return budget


# ---------------------------------------------------------------------------
# Conductor helpers
# ---------------------------------------------------------------------------


def get_app_info(max_retries: int = 6, backoff: int = 5) -> dict:
    """Fetch application info from the conductor."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/get_app", timeout=10)
            resp.raise_for_status()
            info = resp.json()
            logger.info(f"App info: {info}")
            return info
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"get_app attempt {attempt}/{max_retries} failed: {e}")
                time.sleep(backoff)
            else:
                raise


def current_stage() -> str | None:
    try:
        resp = requests.get(f"{CONDUCTOR_URL}/status", timeout=10)
        resp.raise_for_status()
        return resp.json().get("stage")
    except Exception as e:
        logger.debug(f"Status poll error: {e}")
        return None


def wait_for_stage(target_stages: set[str], timeout: int = 300) -> str:
    """Poll the conductor until its stage is in ``target_stages``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stage = current_stage()
        if stage in target_stages:
            logger.info(f"Conductor reached stage: {stage}")
            return stage
        time.sleep(2)
    raise TimeoutError(f"Conductor did not reach {target_stages} within {timeout}s")


def submit_to_conductor(solution: str, stage: str) -> dict:
    """POST /submit. The conductor may hold the request for up to 300 s."""
    logger.info(f"Submitting to conductor ({len(solution)} chars, stage={stage})")
    resp = requests.post(f"{CONDUCTOR_URL}/submit", json={"stage": stage, "solution": solution}, timeout=310)
    if not resp.ok:
        logger.error(f"Submit failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    result = resp.json()
    logger.info(f"Submit response: {result}")
    return result


def is_external_submit(command: str) -> bool:
    """A shell command that talks to the submission endpoint itself."""
    return bool(SUBMIT_ENDPOINT.search(command))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class Transcript:
    """Append-only JSONL record of everything the driver did."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def write(self, record: dict) -> None:
        record = {"ts": datetime.now().astimezone().isoformat(), **record}
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _stream_record(text: str) -> dict:
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
        "text": text[:STORED_STREAM_CHARS],
        "stored_truncated": len(text) > STORED_STREAM_CHARS,
    }


@dataclass
class Session:
    """The whole conversation; re-sent on every step."""

    pass_reasoning: bool = True
    messages: list[dict] = field(default_factory=list)

    def open(self, system: dict, first_turn: str) -> None:
        self.messages = [system, {"role": "user", "content": first_turn}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, reply: Reply) -> None:
        self.messages.append(reply.as_message(pass_reasoning=self.pass_reasoning))

    def add_tool_result(self, call_id: str, text: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": text})

    def last_role(self) -> str | None:
        return self.messages[-1]["role"] if self.messages else None


@dataclass
class StageOutcome:
    stage: str
    text: str | None
    termination_reason: str
    tool_calls_used: int
    model_calls: int
    usage_records: list[dict] = field(default_factory=list)
    mechanism_guard_tripped: bool = False
    wall_seconds: float = 0.0
    tool_counts: dict = field(default_factory=dict)
    plain_text_submission: bool = False
    records: int = 0  # command records written by this stage (the next stage numbers its records after them)

    def summary(self) -> dict:
        return {
            "commands_used": self.tool_calls_used,  # name kept for the result tooling; counts every tool call but submit
            "tool_calls_used": self.tool_calls_used,
            "tool_counts": self.tool_counts,
            "model_calls": self.model_calls,
            "termination_reason": self.termination_reason,
            "wall_seconds": self.wall_seconds,
            "mechanism_guard_tripped": self.mechanism_guard_tripped,
            "plain_text_submission": self.plain_text_submission,
            "usage_metrics": aggregate_usage(self.usage_records),
        }


# ---------------------------------------------------------------------------
# Tools and the step loop
# ---------------------------------------------------------------------------


def make_backend(model: str, reasoning_effort: str | None) -> ApiBackend:
    return ApiBackend(model, reasoning_effort)


def build_toolbox(problem_id: str, work_dir: str) -> tuple[Toolbox, dict]:
    """Local tools per BASELINE_TOOLS, plus the MCP sub-servers when a server is configured."""
    local, servers = parse_tool_selection(TOOLS_RAW)
    toolbox = Toolbox(local=local, command_timeout=COMMAND_TIMEOUT, work_dir=work_dir)
    status: dict = {
        "local": local,
        "mcp_servers": servers,
        "mcp_url": MCP_SERVER_URL or None,
        "mcp_tools": [],
        "mcp_error": None,
    }
    if servers:
        if not MCP_SERVER_URL:
            status["mcp_error"] = "MCP_SERVER_URL is not set"
            logger.warning("MCP tools requested but MCP_SERVER_URL is not set; continuing without them")
        else:
            try:
                mcp = McpToolbox(MCP_SERVER_URL, servers, problem_id)
                status["mcp_tools"] = [spec.name for spec in mcp.connect()]
                toolbox.mcp = mcp
                logger.info(f"MCP tools: {status['mcp_tools']}")
            except Exception as exc:
                status["mcp_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                logger.warning(f"MCP tools unavailable: {status['mcp_error']}")
    return toolbox, status


def execute_tool(
    toolbox: Toolbox, spec: ToolSpec, args: dict, transcript: Transcript, *, stage: str, index: int
) -> str:
    """Run one tool call and record it; returns the (truncated) text the model sees."""
    refused = None
    if spec.name == "bash" and is_external_submit(str(args.get("command", ""))):
        refused = "external_submit"
        text = "refused: the driver does not run commands that call the submission endpoint. Use the submit tool."
        record: dict = {}
    else:
        text, record = toolbox.call(spec, args)
    shown = protocol.truncate(text, OUTPUT_CHARS)
    transcript.write(
        {
            "type": "tool_call",
            "stage": stage,
            "index": index,
            "tool": spec.name,
            "kind": spec.kind,
            "args": json.dumps(args, ensure_ascii=False)[:4000],
            "refused": refused,
            "result": _stream_record(text),
            **record,
        }
    )
    return shown


def run_stage(
    backend: ApiBackend,
    toolbox: Toolbox,
    session: Session,
    transcript: Transcript,
    steps_dir: Path,
    *,
    stage: str,
    mode: str,
    max_calls: int | None,
    call_offset: int = 0,
    index_offset: int = 0,
) -> StageOutcome:
    """Loop until the model submits, or a limit forces the last turn with only the submit tool."""
    submit_spec = protocol.submit_tool_spec(mode, stage)
    started = time.monotonic()
    usage_records: list[dict] = []
    model_calls = 0
    used = 0
    tool_counts: dict[str, int] = {}
    consecutive_failures = 0
    nudges = 0
    guard_warned = False
    guard_tripped = False
    forced: str | None = None
    forced_notice_sent = False
    notice: str | None = None

    def outcome(text: str | None, reason: str, *, plain_text: bool = False) -> StageOutcome:
        return StageOutcome(
            stage,
            text,
            reason,
            used,
            model_calls,
            usage_records,
            guard_tripped,
            round(time.monotonic() - started, 3),
            tool_counts,
            plain_text,
        )

    while True:
        if forced is None:
            if max_calls is not None and used >= max_calls:
                forced = "budget_exhausted"
            elif max_calls is None and used >= HARD_CAP:
                forced = "hard_cap"
            elif time.monotonic() - started > DEADLINE_S:
                forced = "deadline"
        if forced and not forced_notice_sent:
            session.add_user(protocol.forced_submit_turn(forced.replace("_", " ")))
            forced_notice_sent = True
            notice = None
        elif notice:
            session.add_user(notice)
            notice = None
        tools = [submit_spec] if forced else [*toolbox.specs(), submit_spec]

        model_calls += 1
        call_number = call_offset + model_calls
        reply = backend.complete(session.messages, tools, step_dir=steps_dir / f"step_{call_number:02d}")
        usage_records.append(reply.usage)
        transcript.write(
            {
                "type": "model_call",
                "stage": stage,
                "call": call_number,
                "messages": len(session.messages),
                "tools_offered": [t.name for t in tools],
                "forced": forced,
                "latency_s": reply.latency_s,
                "usage": reply.usage,
                "finish_reason": reply.finish_reason,
                "content": (reply.content or "")[:20000],
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments[:4000]} for tc in reply.tool_calls
                ],
                "error": reply.error,
            }
        )
        if reply.error:
            consecutive_failures += 1
            logger.warning(f"Model call {call_number} unusable: {reply.error}")
            if consecutive_failures >= 2:
                text = (reply.content or "").strip() if stage == "diagnosis" else ""
                return outcome(
                    text or (protocol.FALLBACK_DIAGNOSIS if stage == "diagnosis" else ""),
                    "model_failure",
                    plain_text=bool(text),
                )
            if reply.content or reply.tool_calls:  # something came back: keep it and say why it was unusable
                session.add_assistant(reply)
                for tc in reply.tool_calls:
                    session.add_tool_result(tc.id, "not executed: the reply was unusable")
                notice = protocol.UNUSABLE_REPLY_TEXT.format(error=reply.error)
            time.sleep(RETRY_WAIT_S)
            continue
        consecutive_failures = 0
        session.add_assistant(reply)

        if not reply.tool_calls:
            text = (reply.content or "").strip()
            logger.info(f"[{stage}] call {call_number}: no tool call - {text[:120]}")
            if forced:  # like Stratus: the plain answer is the submission
                return outcome(
                    text or (protocol.FALLBACK_DIAGNOSIS if stage == "diagnosis" else ""),
                    f"{forced}_forced_submit",
                    plain_text=True,
                )
            nudges += 1
            if nudges > MAX_NUDGES:
                forced = "no_tool_call"
            else:
                notice = protocol.NUDGE_TEXT
            continue

        submitted: str | None = None
        for tc in reply.tool_calls:
            args, parse_error = parse_arguments(tc.arguments)
            if submitted is not None:
                session.add_tool_result(tc.id, "skipped: the stage ended with the submission above")
                continue
            if tc.name == "submit":
                error = parse_error or protocol.validate_submit(args, mode, stage)
                if error:
                    session.add_tool_result(tc.id, f"rejected: {error}. Call submit again with valid arguments.")
                    logger.info(f"[{stage}] call {call_number}: submit rejected - {error}")
                    continue
                assert args is not None
                hits = protocol.mechanism_guard_hits(args, mode) if stage == "diagnosis" else []
                if hits and not guard_warned:
                    guard_warned = True
                    session.add_tool_result(
                        tc.id, protocol.GUARD_REJECTION_TEXT.format(hits=", ".join(repr(h) for h in hits))
                    )
                    logger.info(f"Submission names a mechanism ({hits}); asking once more")
                    continue
                guard_tripped = bool(hits)
                submitted = protocol.compose_submission(args, mode, stage)
                session.add_tool_result(tc.id, "Submission received.")
                logger.info(f"[{stage}] call {call_number}: submit - {submitted[:160]}")
                continue
            spec = toolbox.find(tc.name) if not forced else None
            if spec is None:
                session.add_tool_result(
                    tc.id,
                    f"error: tool {tc.name!r} is not available" + (" at this point; only submit is" if forced else ""),
                )
                continue
            if max_calls is not None and used >= max_calls:
                session.add_tool_result(tc.id, "skipped: the tool-call budget for this stage is exhausted; call submit")
                continue
            if parse_error:
                session.add_tool_result(tc.id, f"error: {parse_error}")
                continue
            assert args is not None
            used += 1
            tool_counts[tc.name] = tool_counts.get(tc.name, 0) + 1
            logger.info(f"[{stage}] call {call_number}: {tc.name} {json.dumps(args, ensure_ascii=False)[:160]}")
            session.add_tool_result(
                tc.id, execute_tool(toolbox, spec, args, transcript, stage=stage, index=index_offset + used)
            )

        if submitted is not None:
            return outcome(submitted, "submitted" if forced is None else f"{forced}_forced_submit")
        observed = current_stage()
        if observed not in {stage, None}:
            logger.error(f"Conductor stage is {observed} after a tool call; the model bypassed the submit tool")
            return outcome(None, "external_submission")


def run_stage_mini(
    backend: ApiBackend,
    session: Session,
    transcript: Transcript,
    steps_dir: Path,
    *,
    stage: str,
    mode: str,
    max_calls: int | None,
    work_dir: str,
    submit_mode: str = "marker",
    call_offset: int = 0,
    index_offset: int = 0,
) -> StageOutcome:
    """mini-swe-agent's loop: one bash block per reply, one observation per command.

    In ``marker`` mode the stage ends when a command prints the marker line (the driver then submits).
    In ``curl`` mode the model submits itself, exactly as the task instruction says, and the stage ends
    when the conductor reports the next stage after a command.

    A budget arm (``max_calls`` set) gets one notice when the budget is spent and may then only submit.
    An unrestricted arm that runs into the hard cap or the deadline gets one notice to submit what it has
    and ``WRAP_UP_CALLS`` replies to do it in; a stage that still does not submit ends without a submission.
    """
    started = time.monotonic()
    usage_records: list[dict] = []
    model_calls = 0
    used = 0
    consecutive_failures = 0
    format_errors = 0
    guard_warned = False
    guard_tripped = False
    budget_notice_sent = False
    wrap_up_sent = False
    wrap_up_calls = 0
    records = 0

    def outcome(text: str | None, reason: str) -> StageOutcome:
        return StageOutcome(
            stage,
            text,
            reason,
            used,
            model_calls,
            usage_records,
            guard_tripped,
            round(time.monotonic() - started, 3),
            {"bash": used},
            records=records,
        )

    def rejected(reason: str) -> str:
        return mini.REJECTED_COMMAND_OBSERVATION.format(reason=reason)

    def with_budget(text: str) -> str:
        """Unrestricted arms see how much of the stage is left after every command; budget arms
        already carry their allowance in the instance prompt and are left untouched."""
        if max_calls is not None:
            return text
        return text + mini.budget_notice(
            used=used,
            cap=HARD_CAP,
            seconds_left=DEADLINE_S - (time.monotonic() - started),
            submit_mode=submit_mode,
        )

    while True:
        limit = None
        if time.monotonic() - started > DEADLINE_S:
            limit = ("deadline", "the time limit")
        elif max_calls is None and used >= HARD_CAP:
            limit = ("hard_cap", f"the limit of {HARD_CAP} commands")
        if limit is not None:
            if not wrap_up_sent:
                wrap_up_sent = True
                logger.info(f"[{stage}] {limit[0]} reached; asking for a submission")
                transcript.write({"type": "wrap_up", "stage": stage, "reason": limit[0], "commands": used})
                session.add_user(mini.wrap_up_text(limit[1], submit_mode))
            if wrap_up_calls >= WRAP_UP_CALLS:
                return outcome(None, f"{limit[0]}_no_submission")
            wrap_up_calls += 1
        budget_exhausted = max_calls is not None and used >= max_calls
        if budget_exhausted and not budget_notice_sent:
            session.add_user(mini.budget_exhausted_text(max_calls) if max_calls else mini.ZERO_BUDGET_RULE.strip())
            budget_notice_sent = True

        model_calls += 1
        call_number = call_offset + model_calls
        reply = backend.complete(session.messages, [], step_dir=steps_dir / f"step_{call_number:02d}")
        usage_records.append(reply.usage)
        action, n_actions = (None, 0) if reply.error else mini.parse_action(reply.content)
        transcript.write(
            {
                "type": "model_call",
                "stage": stage,
                "call": call_number,
                "messages": len(session.messages),
                "latency_s": reply.latency_s,
                "usage": reply.usage,
                "finish_reason": reply.finish_reason,
                "content": (reply.content or "")[:20000],
                "content_from_reasoning": reply.content_from_reasoning,
                "action": action,
                "n_actions": n_actions,
                "error": reply.error,
            }
        )
        if reply.error:
            consecutive_failures += 1
            logger.warning(f"Model call {call_number} unusable: {reply.error}")
            if consecutive_failures >= 2:
                return outcome(None, "model_failure")
            time.sleep(RETRY_WAIT_S)
            continue
        consecutive_failures = 0
        session.add_assistant(reply)

        if action is None:
            format_errors += 1
            logger.info(f"[{stage}] call {call_number}: format error ({n_actions} actions)")
            if format_errors >= mini.MAX_CONSECUTIVE_FORMAT_ERRORS:
                return outcome(None, "repeated_format_error")
            session.add_user(mini.format_error_text(n_actions))
            continue
        format_errors = 0
        records += 1
        index = index_offset + records

        if budget_exhausted and mini.MARKER not in action:
            session.add_user(
                rejected("the command budget for this stage is spent; only the submission command is allowed")
            )
            transcript.write(
                {
                    "type": "command",
                    "stage": stage,
                    "index": index,
                    "command": action,
                    "refused": "budget_exhausted",
                    "exit_code": 126,
                }
            )
            format_errors += 1
            if format_errors >= mini.MAX_CONSECUTIVE_FORMAT_ERRORS:
                return outcome(None, "budget_exhausted_no_submission")
            continue
        if submit_mode == "marker" and is_external_submit(action):
            used += 1
            session.add_user(
                rejected(
                    "the driver does not run commands that call the submission endpoint; submit with the marker command instead"
                )
            )
            transcript.write(
                {
                    "type": "command",
                    "stage": stage,
                    "index": index,
                    "command": action,
                    "refused": "external_submit",
                    "exit_code": 126,
                }
            )
            continue

        logger.info(f"[{stage}] call {call_number}: {action[:160]}")
        result = run_command(action, COMMAND_TIMEOUT, cwd=work_dir)
        separator = "\n" if result.stdout and not result.stdout.endswith("\n") else ""
        output = result.stdout + (separator + result.stderr if result.stderr else "")
        submission = None if (result.timed_out or submit_mode == "curl") else mini.finished(output, result.exit_code)
        transcript.write(
            {
                "type": "command",
                "stage": stage,
                "index": index,
                "command": action,
                "exit_code": result.exit_code,
                "duration_s": result.duration_s,
                "timed_out": result.timed_out,
                "refused": None,
                "submission_attempt": submission is not None,
                "stdout": _stream_record(result.stdout),
                "stderr": _stream_record(result.stderr),
            }
        )
        if result.timed_out:
            used += 1
            session.add_user(with_budget(mini.timeout_text(action, output)))
            continue
        if submission is None:
            used += 1
            session.add_user(with_budget(mini.observation_text(result.exit_code, output)))
            observed = current_stage()
            if observed not in {stage, None}:
                if submit_mode == "curl":
                    logger.info(f"[{stage}] the model submitted with a command; conductor stage is now {observed}")
                    return outcome(None, "submitted_by_command")
                logger.error(
                    f"Conductor stage is {observed} after a command; the model bypassed the submission command"
                )
                return outcome(None, "external_submission")
            continue

        # The marker was printed: this command is the submission attempt.
        if stage == "mitigation":
            return outcome("", "submitted")
        if mode == "full":
            if not submission:
                session.add_user(
                    rejected("the diagnosis after the marker line is empty; print your diagnosis after it")
                )
                continue
            return outcome(submission, "submitted")
        fields, error = mini.parse_three_fields(submission)
        if error:
            session.add_user(rejected(error))
            continue
        assert fields is not None
        hits = mini.mechanism_guard_hits(fields)
        if hits and not guard_warned:
            guard_warned = True
            session.add_user(
                rejected(mini.GUARD_REJECTION.format(hits=", ".join(repr(h) for h in hits))[len("rejected: ") :])
            )
            logger.info(f"Submission names a mechanism ({hits}); asking once more")
            continue
        guard_tripped = bool(hits)
        return outcome(mini.compose_three_fields(fields), "submitted")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def save_results(path: Path, problem_id: str, return_code: int, usage: dict, baseline: dict) -> None:
    payload = {
        "problem_id": problem_id,
        "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "return_code": return_code,
        "success": return_code == EXIT_OK,
        "usage_metrics": usage,
        "baseline": baseline,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Saved results to {path}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_preflight() -> None:
    """Validate the model path with one minimal call (no tools; the MCP server is deployed later)."""
    backend = make_backend(MODEL, REASONING_EFFORT)
    with tempfile.TemporaryDirectory(prefix="baseline-preflight-") as tmp:
        reply = backend.complete(
            [backend.system_message("Reply with the single word ok."), {"role": "user", "content": "ok?"}],
            [],
            step_dir=Path(tmp) / "preflight",
        )
        if reply.error:
            print(reply.error)
            sys.exit(1)
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Run the SREGym baseline agent")
    parser.add_argument("--logs-dir", default=AGENT_LOGS_DIR)
    parser.add_argument("--problem-id", default=None, help="artifact id (default: SREGYM_ARTIFACT_ID)")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    problem_id = resolve_problem_id(cli_problem_id=args.problem_id)
    try:
        max_calls = parse_budget(MAX_COMMANDS_RAW)
        parse_tool_selection(TOOLS_RAW)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(EXIT_INFRA)
    if SUBMISSION_MODE not in protocol.SUBMISSION_MODES:
        logger.error(f"BASELINE_SUBMISSION_MODE must be one of {protocol.SUBMISSION_MODES}")
        sys.exit(EXIT_INFRA)
    if PROTOCOL not in PROTOCOLS:
        logger.error(f"BASELINE_PROTOCOL must be one of {PROTOCOLS}")
        sys.exit(EXIT_INFRA)
    try:
        submit_mode = (
            mini.resolve_submit_mode(SUBMIT_RAW, mode=SUBMISSION_MODE, max_commands=max_calls)
            if PROTOCOL == "mini"
            else "tool"
        )
    except ValueError as e:
        logger.error(str(e))
        sys.exit(EXIT_INFRA)

    transcript = Transcript(logs_dir / "baseline_transcript.jsonl")
    results_path = logs_dir / f"baseline_results_{problem_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backend = make_backend(MODEL, REASONING_EFFORT)
    try:
        backend_version = backend.version()
    except Exception as e:
        backend_version = f"unavailable: {e}"
    if PROTOCOL == "tools":
        toolbox, tool_status = build_toolbox(problem_id, str(logs_dir))
    else:
        toolbox, tool_status = (
            None,
            {"protocol": "mini", "local": ["bash"], "mcp_servers": [], "mcp_tools": [], "mcp_error": None},
        )
    baseline_meta: dict = {
        "backend": backend.name,
        "backend_version": backend_version,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "protocol": PROTOCOL,
        "submit_mode": submit_mode,
        "context": "session",
        "session_reasoning": SESSION_REASONING,
        "max_commands": "unlimited" if max_calls is None else max_calls,
        "submission_mode": SUBMISSION_MODE,
        "tools": tool_status,
        "hard_cap": HARD_CAP,
        "deadline_s": DEADLINE_S,
        "output_chars": OUTPUT_CHARS,
        "artifact_id": problem_id,
        "stages": {},
    }
    transcript.write({"type": "meta", **baseline_meta, "conductor_url": CONDUCTOR_URL})
    logger.info("=" * 60)
    logger.info(f"Baseline driver starting: {baseline_meta}")
    logger.info("=" * 60)

    try:
        stage = wait_for_stage({"diagnosis", "mitigation"}, timeout=300)
    except TimeoutError:
        logger.error("Timed out waiting for the conductor to reach an agent stage")
        sys.exit(EXIT_INFRA)
    try:
        app_info = get_app_info()
    except Exception as e:
        logger.error(f"Failed to get app info: {e}")
        sys.exit(EXIT_INFRA)

    task_text = protocol.build_task_text(app_info)
    session = Session(pass_reasoning=SESSION_REASONING)
    if PROTOCOL == "mini":
        session.open(
            backend.system_message(mini.system_text()),
            mini.instance_text(app_info, mode=SUBMISSION_MODE, max_commands=max_calls, submit_mode=submit_mode),
        )
        if stage == "mitigation" and submit_mode == "marker":
            session.add_user(mini.mitigation_text(max_calls))
    else:
        session.open(
            backend.system_message(protocol.system_text(SUBMISSION_MODE)),
            protocol.opening_turn(task_text, stage=stage, max_calls=max_calls),
        )

    def run(stage_name: str, **kwargs) -> StageOutcome:
        if PROTOCOL == "mini":
            return run_stage_mini(
                backend,
                session,
                transcript,
                logs_dir / "steps",
                stage=stage_name,
                mode=SUBMISSION_MODE,
                max_calls=max_calls,
                work_dir=str(logs_dir),
                submit_mode=submit_mode,
                **kwargs,
            )
        assert toolbox is not None
        return run_stage(
            backend,
            toolbox,
            session,
            transcript,
            logs_dir / "steps",
            stage=stage_name,
            mode=SUBMISSION_MODE,
            max_calls=max_calls,
            **kwargs,
        )

    no_submission = {
        "deadline_no_submission",
        "hard_cap_no_submission",
        "repeated_format_error",
        "budget_exhausted_no_submission",
        "model_failure",
    }

    return_code = EXIT_OK
    usage_records: list[dict] = []
    submitted_stages: list[str] = []
    calls = 0
    tool_calls = 0

    def finish_snapshot() -> None:
        baseline_meta["submitted_stages"] = list(submitted_stages)
        save_results(results_path, problem_id, return_code, aggregate_usage(usage_records), baseline_meta)

    if stage == "diagnosis":
        outcome = run("diagnosis")
        usage_records.extend(outcome.usage_records)
        calls = outcome.model_calls
        tool_calls = outcome.records if PROTOCOL == "mini" else outcome.tool_calls_used
        baseline_meta["stages"]["diagnosis"] = outcome.summary()
        # Top-level copies keep the diagnosis-only fields where earlier runs put them.
        baseline_meta.update(
            commands_used=outcome.tool_calls_used,
            model_calls=outcome.model_calls,
            termination_reason=outcome.termination_reason,
            wall_seconds=outcome.wall_seconds,
            mechanism_guard_tripped=outcome.mechanism_guard_tripped,
        )
        if outcome.termination_reason == "submitted_by_command":
            submitted_stages.append("diagnosis")
            transcript.write({"type": "submit", "stage": "diagnosis", "by": "command"})
        elif outcome.termination_reason == "external_submission":
            return_code = EXIT_EXTERNAL_SUBMISSION
        elif outcome.termination_reason in no_submission:
            # Like a CLI agent that stops without submitting: nothing is sent and the run ends here.
            return_code = EXIT_MODEL_FAILURE if outcome.termination_reason == "model_failure" else EXIT_NO_SUBMISSION
            logger.warning(f"Diagnosis ended without a submission ({outcome.termination_reason}); exiting")
        else:
            try:
                response = submit_to_conductor(outcome.text or "", "diagnosis")
                submitted_stages.append("diagnosis")
                transcript.write({"type": "submit", "stage": "diagnosis", "text": outcome.text, "response": response})
            except Exception as e:
                logger.error(f"Diagnosis submission failed: {e}")
                transcript.write({"type": "submit", "stage": "diagnosis", "text": outcome.text, "error": str(e)})
                return_code = EXIT_INFRA
            if return_code == EXIT_OK and outcome.termination_reason == "model_failure":
                return_code = EXIT_MODEL_FAILURE
        transcript.write({"type": "end", "stage": "diagnosis", "reason": outcome.termination_reason})
        finish_snapshot()
        if "diagnosis" not in submitted_stages:
            stage = None
        else:
            try:
                stage = wait_for_stage({"mitigation", "tearing_down", "done"}, timeout=600)
            except TimeoutError:
                logger.warning("Timed out waiting for the stage after diagnosis")
                stage = None
        if stage == "mitigation" and submit_mode != "curl":  # in curl mode the task text already covers both stages
            session.add_user(
                mini.mitigation_text(max_calls) if PROTOCOL == "mini" else protocol.stage_change_turn(max_calls)
            )
    else:
        logger.info("Benchmark starts at mitigation; skipping diagnosis")

    if stage == "mitigation":
        outcome = run("mitigation", call_offset=calls, index_offset=tool_calls)
        usage_records.extend(outcome.usage_records)
        baseline_meta["stages"]["mitigation"] = outcome.summary()
        if outcome.termination_reason == "submitted_by_command":
            submitted_stages.append("mitigation")
            transcript.write({"type": "submit", "stage": "mitigation", "by": "command"})
        elif outcome.termination_reason == "external_submission":
            if return_code == EXIT_OK:
                return_code = EXIT_EXTERNAL_SUBMISSION
        elif outcome.termination_reason in no_submission:
            logger.warning(f"Mitigation ended without a submission ({outcome.termination_reason}); exiting")
            if return_code == EXIT_OK:
                return_code = (
                    EXIT_MODEL_FAILURE if outcome.termination_reason == "model_failure" else EXIT_NO_SUBMISSION
                )
        else:
            try:
                response = submit_to_conductor("", "mitigation")
                submitted_stages.append("mitigation")
                transcript.write({"type": "submit", "stage": "mitigation", "text": "", "response": response})
            except Exception as e:
                logger.error(f"Mitigation submission failed: {e}")
                transcript.write({"type": "submit", "stage": "mitigation", "text": "", "error": str(e)})
                if return_code == EXIT_OK:
                    return_code = EXIT_INFRA
        transcript.write({"type": "end", "stage": "mitigation", "reason": outcome.termination_reason})
        finish_snapshot()
        with contextlib.suppress(TimeoutError):
            wait_for_stage({"tearing_down", "done"}, timeout=600)

    finish_snapshot()
    transcript.write({"type": "end", "stage": "run", "return_code": return_code})
    transcript.close()
    if toolbox is not None and toolbox.mcp is not None:
        toolbox.mcp.close()
    logger.info(f"Baseline driver finished with return code {return_code}")
    sys.exit(return_code)


if __name__ == "__main__":
    main()
