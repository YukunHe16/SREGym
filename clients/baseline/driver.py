"""
Baseline agent driver for SREGym.

The simplest examinee: the same task instruction as the CLI agents, a loop that
asks the model for one shell command at a time, runs it, feeds the output back,
and stops when the model submits or the command budget is exhausted. Two knobs
make it a controlled examinee:

    BASELINE_MAX_COMMANDS      0, a positive integer, or "unlimited" (per stage)
    BASELINE_SUBMISSION_MODE   "full" or "no_mechanism"

With a budget of 0 the model sees only the task description and must submit at
once (the zero-action agent). In ``no_mechanism`` mode the diagnosis submission
is assembled from three fields (faulty component, affected components, symptom)
and may not explain why the fault happens. In the mitigation stage the same loop
runs with the diagnosis transcript carried over; ``submit`` there means "my fix
is applied" and sends the empty submission that triggers validation.
"""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
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

from clients.baseline import protocol  # noqa: E402
from clients.baseline.backends import ApiBackend, CodexExecBackend, ModelBackend, StepResult  # noqa: E402
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
MODEL = os.environ.get("AGENT_MODEL_ID", "gpt-5.5")
REASONING_EFFORT = os.environ.get("AGENT_REASONING_EFFORT")

BACKEND_NAME = os.environ.get("BASELINE_BACKEND", "")
MAX_COMMANDS_RAW = os.environ.get("BASELINE_MAX_COMMANDS", "10")
SUBMISSION_MODE = os.environ.get("BASELINE_SUBMISSION_MODE", "full")
HARD_CAP = int(os.environ.get("BASELINE_HARD_CAP", "60"))
COMMAND_TIMEOUT = int(os.environ.get("BASELINE_COMMAND_TIMEOUT", "60"))
OUTPUT_CHARS = int(os.environ.get("BASELINE_OUTPUT_CHARS", "8000"))
DEADLINE_S = float(os.environ.get("BASELINE_DEADLINE_S", "840"))
RETRY_WAIT_S = float(os.environ.get("BASELINE_RETRY_WAIT_S", "30"))
# stateless: every step is a fresh call that re-reads the whole transcript (the model's own reasoning is
# not carried). session: one growing conversation; the model's replies and, when the provider returns
# it, its reasoning are sent back each step, so the prefix stays cacheable and nothing is re-derived.
CONTEXT = os.environ.get("BASELINE_CONTEXT", "stateless")
CONTEXT_MODES = ("stateless", "session")
SESSION_REASONING = os.environ.get("BASELINE_SESSION_REASONING", "1") != "0"

STORED_STREAM_CHARS = 256 * 1024
FALLBACK_DIAGNOSIS = "No diagnosis could be produced."
SUBMIT_ENDPOINT = re.compile(r"/submit(?:_mcp)?\b")

EXIT_OK = 0
EXIT_INFRA = 1
EXIT_MODEL_FAILURE = 2
EXIT_EXTERNAL_SUBMISSION = 3


def parse_budget(value: str) -> int | None:
    """``None`` means unlimited (bounded only by the hard cap and the deadline)."""
    text = str(value).strip().lower()
    if text in {"unlimited", "none", "inf", ""}:
        return None
    budget = int(text)
    if budget < 0:
        raise ValueError("BASELINE_MAX_COMMANDS must be 0, a positive integer, or unlimited")
    return budget


def default_backend_name() -> str:
    """Explicit BASELINE_BACKEND wins; otherwise a provider endpoint means the API path, else the Codex CLI."""
    if BACKEND_NAME:
        return BACKEND_NAME
    if os.environ.get("AGENT_API_BASE") or os.environ.get("AGENT_API_KEY"):
        return "api"
    return "codex"


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
    start = time.time()
    while time.time() - start < timeout:
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


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int | None
    duration_s: float
    timed_out: bool = False


def run_command(command: str, timeout: int, cwd: str | None = None) -> CommandResult:
    """Run one command line through bash; kill the whole process group on timeout."""
    started = time.monotonic()
    with subprocess.Popen(
        ["bash", "-lc", command],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            timed_out = True
        exit_code = 124 if timed_out else process.returncode
    return CommandResult(stdout or "", stderr or "", exit_code, round(time.monotonic() - started, 3), timed_out)


def is_external_submit(command: str) -> bool:
    """The model must submit through the protocol, not by calling the conductor itself."""
    return SUBMIT_ENDPOINT.search(command) is not None


# ---------------------------------------------------------------------------
# Transcript
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


# ---------------------------------------------------------------------------
# Stage loop (diagnosis and mitigation share it)
# ---------------------------------------------------------------------------


@dataclass
class StageOutcome:
    stage: str
    text: str | None
    termination_reason: str
    commands_used: int
    model_calls: int
    usage_records: list[dict] = field(default_factory=list)
    mechanism_guard_tripped: bool = False
    wall_seconds: float = 0.0
    steps: list[protocol.StepRecord] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "commands_used": self.commands_used,
            "model_calls": self.model_calls,
            "termination_reason": self.termination_reason,
            "wall_seconds": self.wall_seconds,
            "mechanism_guard_tripped": self.mechanism_guard_tripped,
            "usage_metrics": aggregate_usage(self.usage_records),
        }


@dataclass
class Session:
    """Session mode: the driver keeps the whole conversation and re-sends it on every step.

    Assistant turns carry the model's raw JSON reply and, when ``pass_reasoning`` is on and the
    provider returned one, its ``reasoning_content`` (Z.ai keeps it in context with
    ``thinking.clear_thinking=false``; providers that reject the field need ``BASELINE_SESSION_REASONING=0``).
    """

    pass_reasoning: bool = True
    messages: list[dict] = field(default_factory=list)

    def open(self, system: dict, first_turn: str) -> None:
        self.messages = [system, {"role": "user", "content": first_turn}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, raw: str, reasoning: str | None) -> None:
        message: dict = {"role": "assistant", "content": raw}
        if self.pass_reasoning and reasoning:
            message["reasoning_content"] = reasoning
        self.messages.append(message)

    def last_role(self) -> str | None:
        return self.messages[-1]["role"] if self.messages else None


def make_backend(name: str, model: str, reasoning_effort: str | None) -> ModelBackend:
    if name == "codex":
        return CodexExecBackend(model, reasoning_effort)
    if name == "api":
        return ApiBackend(model, reasoning_effort)
    raise ValueError(f"Unknown BASELINE_BACKEND: {name}")


def execute_step(command: str, index: int, transcript: Transcript, cwd: str, stage: str) -> protocol.StepRecord:
    if is_external_submit(command):
        refusal = 'The driver does not run commands that call the submission endpoint. Use action "submit".'
        record = protocol.StepRecord(index, command, 126, "", refusal, refused="external_submit")
    else:
        result = run_command(command, COMMAND_TIMEOUT, cwd=cwd)
        record = protocol.StepRecord(
            index, command, result.exit_code, result.stdout, result.stderr, result.duration_s, result.timed_out
        )
    transcript.write(
        {
            "type": "command",
            "stage": stage,
            "step": index,
            "command": command,
            "exit_code": record.exit_code,
            "duration_s": record.duration_s,
            "timed_out": record.timed_out,
            "refused": record.refused,
            "stdout": _stream_record(record.stdout),
            "stderr": _stream_record(record.stderr),
        }
    )
    return record


def run_stage(
    backend: ModelBackend,
    app_info: dict,
    transcript: Transcript,
    steps_dir: Path,
    *,
    stage: str,
    mode: str,
    max_commands: int | None,
    work_dir: str,
    prior_steps: list[protocol.StepRecord] | None = None,
    call_offset: int = 0,
    session: Session | None = None,
) -> StageOutcome:
    """Ask for one command at a time until the model submits or the budget forces a submission.

    ``prior_steps`` (the diagnosis transcript) is shown but not counted in the mitigation stage.
    With ``session`` the conversation continues across steps and stages instead of being rebuilt.
    """
    task_text = protocol.build_task_text(app_info)
    prior = list(prior_steps or [])
    steps: list[protocol.StepRecord] = []
    usage_records: list[dict] = []
    model_calls = 0
    consecutive_failures = 0
    violation: list[str] | None = None
    guard_tripped = False
    started = time.monotonic()

    def outcome(text: str | None, reason: str) -> StageOutcome:
        return StageOutcome(
            stage,
            text,
            reason,
            len(steps),
            model_calls,
            usage_records,
            guard_tripped,
            round(time.monotonic() - started, 3),
            steps,
        )

    schema_fixed = protocol.step_schema(mode, False)
    stage_change = protocol.stage_change_text(stage) if session is not None and session.messages else None
    last_step: protocol.StepRecord | None = None
    unusable: str | None = None

    while True:
        used = len(steps)
        forced: str | None = None
        if max_commands is not None and used >= max_commands:
            forced = "budget_exhausted"
        elif max_commands is None and used >= HARD_CAP:
            forced = "hard_cap"
        elif time.monotonic() - started > DEADLINE_S:
            forced = "deadline"
        submit_only = forced is not None
        model_calls += 1
        call_number = call_offset + model_calls
        step_dir = steps_dir / f"step_{call_number:02d}"

        if session is None:
            prompt = protocol.build_step_prompt(
                task_text,
                mode=mode,
                max_commands=max_commands,
                used=used,
                steps=prior + steps,
                submit_only=submit_only,
                output_chars=OUTPUT_CHARS,
                violation=violation,
                stage=stage,
            )
            request_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            result: StepResult = backend.complete_json(
                prompt, protocol.step_schema(mode, submit_only), step_dir=step_dir
            )
        else:
            if not session.messages:
                prompt = protocol.build_step_prompt(
                    task_text,
                    mode=mode,
                    max_commands=max_commands,
                    used=used,
                    steps=prior + steps,
                    submit_only=submit_only,
                    output_chars=OUTPUT_CHARS,
                    violation=violation,
                    stage=stage,
                )
                session.open(backend.system_message(schema_fixed), prompt)
            elif session.last_role() == "assistant":
                prompt = protocol.build_session_turn(
                    last_step,
                    max_commands=max_commands,
                    used=used,
                    next_step=len(prior) + used + 1,
                    submit_only=submit_only,
                    output_chars=OUTPUT_CHARS,
                    violation=violation,
                    stage_change=stage_change,
                    unusable_reply=unusable,
                )
                session.add_user(prompt)
            else:  # the last call failed before the model answered: resend the conversation as it is
                prompt = str(session.messages[-1]["content"])
            stage_change = None
            last_step = None
            unusable = None
            request_sha = hashlib.sha256(json.dumps(session.messages, ensure_ascii=False).encode("utf-8")).hexdigest()
            result = backend.complete_messages(session.messages, step_dir=step_dir)
            if result.raw_text:
                session.add_assistant(result.raw_text, result.reasoning)
        usage_records.append(result.usage)
        error = result.error or protocol.validate_step(result.parsed, mode, submit_only, stage)
        transcript.write(
            {
                "type": "model_call",
                "stage": stage,
                "call": call_number,
                "context": "stateless" if session is None else "session",
                "messages": None if session is None else len(session.messages),
                "commands_used": used,
                "submit_only": submit_only,
                "forced": forced,
                "violation_notice": violation,
                "prompt_chars": len(prompt),
                "prompt_sha256": request_sha,
                "latency_s": result.latency_s,
                "usage": result.usage,
                "raw": result.raw_text[:20000],
                "parsed": result.parsed,
                "error": error,
            }
        )
        if error:
            consecutive_failures += 1
            logger.warning(f"Model call {call_number} unusable: {error}")
            if consecutive_failures >= 2:
                return outcome(FALLBACK_DIAGNOSIS if stage == "diagnosis" else "", "model_failure")
            if session is not None and result.raw_text:
                unusable = error
            time.sleep(RETRY_WAIT_S)
            continue
        consecutive_failures = 0
        parsed = result.parsed
        assert parsed is not None
        logger.info(f"[{stage}] call {call_number}: {parsed.get('action')} - {parsed.get('note', '')}")

        if parsed["action"] == "submit":
            hits = protocol.mechanism_guard_hits(parsed, mode) if stage == "diagnosis" else []
            if hits and violation is None:
                logger.info(f"Submission names a mechanism ({hits}); asking once more")
                violation = hits
                continue
            guard_tripped = bool(hits)
            reason = "submitted" if forced is None else f"{forced}_forced_submit"
            return outcome(protocol.compose_submission(parsed, mode, stage), reason)

        violation = None
        index = len(prior) + used + 1
        steps.append(execute_step(parsed["command"], index, transcript, work_dir, stage))
        last_step = steps[-1]
        observed = current_stage()
        if observed not in {stage, None}:
            logger.error(f"Conductor stage is {observed} after a command; the model bypassed the submit action")
            return outcome(None, "external_submission")


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
    """Validate the model path with one minimal schema-constrained call."""
    backend = make_backend(default_backend_name(), MODEL, REASONING_EFFORT)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    with tempfile.TemporaryDirectory(prefix="baseline-preflight-") as tmp:
        result = backend.complete_json('Return exactly {"ok": true}.', schema, step_dir=Path(tmp) / "preflight")
        if result.error or not isinstance(result.parsed, dict) or result.parsed.get("ok") is not True:
            print(result.error or f"unexpected preflight reply: {result.raw_text[:500]}")
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
        max_commands = parse_budget(MAX_COMMANDS_RAW)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(EXIT_INFRA)
    if SUBMISSION_MODE not in protocol.SUBMISSION_MODES:
        logger.error(f"BASELINE_SUBMISSION_MODE must be one of {protocol.SUBMISSION_MODES}")
        sys.exit(EXIT_INFRA)
    if CONTEXT not in CONTEXT_MODES:
        logger.error(f"BASELINE_CONTEXT must be one of {CONTEXT_MODES}")
        sys.exit(EXIT_INFRA)

    transcript = Transcript(logs_dir / "baseline_transcript.jsonl")
    results_path = logs_dir / f"baseline_results_{problem_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backend_name = default_backend_name()
    backend = make_backend(backend_name, MODEL, REASONING_EFFORT)
    try:
        backend_version = backend.version()
    except Exception as e:
        backend_version = f"unavailable: {e}"
    session: Session | None = None
    if CONTEXT == "session":
        if not hasattr(backend, "complete_messages"):
            logger.error(
                f"BASELINE_CONTEXT=session needs a backend that accepts a message list; {backend.name} does not"
            )
            sys.exit(EXIT_INFRA)
        session = Session(pass_reasoning=SESSION_REASONING)
    baseline_meta: dict = {
        "backend": backend.name,
        "backend_version": backend_version,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "context": CONTEXT,
        "session_reasoning": SESSION_REASONING if session is not None else None,
        "max_commands": "unlimited" if max_commands is None else max_commands,
        "submission_mode": SUBMISSION_MODE,
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

    return_code = EXIT_OK
    usage_records: list[dict] = []
    submitted_stages: list[str] = []
    diagnosis_steps: list[protocol.StepRecord] = []
    calls = 0

    def finish_snapshot() -> None:
        baseline_meta["submitted_stages"] = list(submitted_stages)
        save_results(results_path, problem_id, return_code, aggregate_usage(usage_records), baseline_meta)

    if stage == "diagnosis":
        outcome = run_stage(
            backend,
            app_info,
            transcript,
            logs_dir / "steps",
            stage="diagnosis",
            mode=SUBMISSION_MODE,
            max_commands=max_commands,
            work_dir=str(logs_dir),
            session=session,
        )
        usage_records.extend(outcome.usage_records)
        diagnosis_steps = outcome.steps
        calls = outcome.model_calls
        baseline_meta["stages"]["diagnosis"] = outcome.summary()
        # Top-level copies keep the diagnosis-only fields where earlier runs put them.
        baseline_meta.update(
            commands_used=outcome.commands_used,
            model_calls=outcome.model_calls,
            termination_reason=outcome.termination_reason,
            wall_seconds=outcome.wall_seconds,
            mechanism_guard_tripped=outcome.mechanism_guard_tripped,
        )
        if outcome.termination_reason == "external_submission":
            return_code = EXIT_EXTERNAL_SUBMISSION
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
        try:
            stage = wait_for_stage({"mitigation", "tearing_down", "done"}, timeout=600)
        except TimeoutError:
            logger.warning("Timed out waiting for the stage after diagnosis")
            stage = None
    else:
        logger.info("Benchmark starts at mitigation; skipping diagnosis")

    if stage == "mitigation":
        outcome = run_stage(
            backend,
            app_info,
            transcript,
            logs_dir / "steps",
            stage="mitigation",
            mode=SUBMISSION_MODE,
            max_commands=max_commands,
            work_dir=str(logs_dir),
            prior_steps=diagnosis_steps,
            call_offset=calls,
            session=session,
        )
        usage_records.extend(outcome.usage_records)
        baseline_meta["stages"]["mitigation"] = outcome.summary()
        if outcome.termination_reason == "external_submission":
            if return_code == EXIT_OK:
                return_code = EXIT_EXTERNAL_SUBMISSION
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
    logger.info(f"Baseline driver finished with return code {return_code}")
    sys.exit(return_code)


if __name__ == "__main__":
    main()
