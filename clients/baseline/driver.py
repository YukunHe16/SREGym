"""
Baseline agent driver for SREGym.

The simplest examinee: the same task instruction as the CLI agents, a loop that
asks the model for one shell command at a time, runs it, feeds the output back,
and stops when the model submits or the command budget is exhausted. Two knobs
make it a controlled examinee:

    BASELINE_MAX_COMMANDS      0, a positive integer, or "unlimited"
    BASELINE_SUBMISSION_MODE   "full" or "no_mechanism"

With a budget of 0 the model sees only the task description and must submit at
once (the zero-action agent). In ``no_mechanism`` mode the submission is
assembled from three fields (faulty component, affected components, symptom)
and may not explain why the fault happens.
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

BACKEND_NAME = os.environ.get("BASELINE_BACKEND", "codex")
MAX_COMMANDS_RAW = os.environ.get("BASELINE_MAX_COMMANDS", "10")
SUBMISSION_MODE = os.environ.get("BASELINE_SUBMISSION_MODE", "full")
HARD_CAP = int(os.environ.get("BASELINE_HARD_CAP", "60"))
COMMAND_TIMEOUT = int(os.environ.get("BASELINE_COMMAND_TIMEOUT", "60"))
OUTPUT_CHARS = int(os.environ.get("BASELINE_OUTPUT_CHARS", "8000"))
DEADLINE_S = float(os.environ.get("BASELINE_DEADLINE_S", "840"))
RETRY_WAIT_S = float(os.environ.get("BASELINE_RETRY_WAIT_S", "30"))

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
# Diagnosis loop
# ---------------------------------------------------------------------------


@dataclass
class DiagnosisOutcome:
    text: str | None
    termination_reason: str
    commands_used: int
    model_calls: int
    usage_records: list[dict] = field(default_factory=list)
    mechanism_guard_tripped: bool = False
    wall_seconds: float = 0.0


def make_backend(name: str, model: str, reasoning_effort: str | None) -> ModelBackend:
    if name == "codex":
        return CodexExecBackend(model, reasoning_effort)
    if name == "api":
        return ApiBackend(model, reasoning_effort)
    raise ValueError(f"Unknown BASELINE_BACKEND: {name}")


def execute_step(command: str, index: int, transcript: Transcript, cwd: str) -> protocol.StepRecord:
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


def run_diagnosis(
    backend: ModelBackend,
    app_info: dict,
    transcript: Transcript,
    steps_dir: Path,
    *,
    mode: str,
    max_commands: int | None,
    work_dir: str,
) -> DiagnosisOutcome:
    """Ask for one command at a time until the model submits or the budget forces a submission."""
    task_text = protocol.build_task_text(app_info)
    steps: list[protocol.StepRecord] = []
    usage_records: list[dict] = []
    model_calls = 0
    consecutive_failures = 0
    violation: list[str] | None = None
    guard_tripped = False
    started = time.monotonic()

    def outcome(text: str | None, reason: str) -> DiagnosisOutcome:
        return DiagnosisOutcome(
            text,
            reason,
            len(steps),
            model_calls,
            usage_records,
            guard_tripped,
            round(time.monotonic() - started, 3),
        )

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

        prompt = protocol.build_step_prompt(
            task_text,
            mode=mode,
            max_commands=max_commands,
            used=used,
            steps=steps,
            submit_only=submit_only,
            output_chars=OUTPUT_CHARS,
            violation=violation,
        )
        schema = protocol.step_schema(mode, submit_only)
        model_calls += 1
        result: StepResult = backend.complete_json(prompt, schema, step_dir=steps_dir / f"step_{model_calls:02d}")
        usage_records.append(result.usage)
        error = result.error or protocol.validate_step(result.parsed, mode, submit_only)
        transcript.write(
            {
                "type": "model_call",
                "call": model_calls,
                "commands_used": used,
                "submit_only": submit_only,
                "forced": forced,
                "violation_notice": violation,
                "prompt_chars": len(prompt),
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "latency_s": result.latency_s,
                "usage": result.usage,
                "raw": result.raw_text[:20000],
                "parsed": result.parsed,
                "error": error,
            }
        )
        if error:
            consecutive_failures += 1
            logger.warning(f"Model call {model_calls} unusable: {error}")
            if consecutive_failures >= 2:
                return outcome(FALLBACK_DIAGNOSIS, "model_failure")
            time.sleep(RETRY_WAIT_S)
            continue
        consecutive_failures = 0
        parsed = result.parsed
        assert parsed is not None
        logger.info(f"Step {model_calls}: {parsed.get('action')} - {parsed.get('note', '')}")

        if parsed["action"] == "submit":
            hits = protocol.mechanism_guard_hits(parsed, mode)
            if hits and violation is None:
                logger.info(f"Submission names a mechanism ({hits}); asking once more")
                violation = hits
                continue
            guard_tripped = bool(hits)
            reason = "submitted" if forced is None else f"{forced}_forced_submit"
            return outcome(protocol.compose_submission(parsed, mode), reason)

        violation = None
        steps.append(execute_step(parsed["command"], used + 1, transcript, work_dir))
        stage = current_stage()
        if stage not in {"diagnosis", None}:
            logger.error(f"Conductor stage is {stage} after a command; the model bypassed the submit action")
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
    backend = make_backend(BACKEND_NAME, MODEL, REASONING_EFFORT)
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

    transcript = Transcript(logs_dir / "baseline_transcript.jsonl")
    results_path = logs_dir / f"baseline_results_{problem_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backend = make_backend(BACKEND_NAME, MODEL, REASONING_EFFORT)
    try:
        backend_version = backend.version()
    except Exception as e:
        backend_version = f"unavailable: {e}"
    baseline_meta = {
        "backend": backend.name,
        "backend_version": backend_version,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "max_commands": "unlimited" if max_commands is None else max_commands,
        "submission_mode": SUBMISSION_MODE,
        "hard_cap": HARD_CAP,
        "deadline_s": DEADLINE_S,
        "output_chars": OUTPUT_CHARS,
        "artifact_id": problem_id,
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
    usage = aggregate_usage([])
    submitted_stages: list[str] = []

    if stage == "diagnosis":
        outcome = run_diagnosis(
            backend,
            app_info,
            transcript,
            logs_dir / "steps",
            mode=SUBMISSION_MODE,
            max_commands=max_commands,
            work_dir=str(logs_dir),
        )
        usage = aggregate_usage(outcome.usage_records)
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
        baseline_meta["submitted_stages"] = list(submitted_stages)
        save_results(results_path, problem_id, return_code, usage, baseline_meta)
        try:
            stage = wait_for_stage({"mitigation", "tearing_down", "done"}, timeout=600)
        except TimeoutError:
            logger.warning("Timed out waiting for the stage after diagnosis")
            stage = None
    else:
        logger.info("Benchmark starts at mitigation; skipping diagnosis")

    if stage == "mitigation":
        # The baseline does not attempt a fix; the empty submission triggers validation.
        try:
            response = submit_to_conductor("", "mitigation")
            submitted_stages.append("mitigation")
            transcript.write({"type": "submit", "stage": "mitigation", "text": "", "response": response})
        except Exception as e:
            logger.error(f"Mitigation submission failed: {e}")
            transcript.write({"type": "submit", "stage": "mitigation", "text": "", "error": str(e)})
            if return_code == EXIT_OK:
                return_code = EXIT_INFRA
        with contextlib.suppress(TimeoutError):
            wait_for_stage({"tearing_down", "done"}, timeout=600)

    baseline_meta["submitted_stages"] = submitted_stages
    save_results(results_path, problem_id, return_code, usage, baseline_meta)
    transcript.write({"type": "end", "stage": "run", "return_code": return_code})
    transcript.close()
    logger.info(f"Baseline driver finished with return code {return_code}")
    sys.exit(return_code)


if __name__ == "__main__":
    main()
