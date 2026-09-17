"""Model backends for the baseline agent: one JSON-constrained completion per step.

Two backends share one interface:

* ``CodexExecBackend`` runs ``codex exec`` once per step with ``--output-schema``.
  Every call is stateless, so the driver resends the transcript. It works with a
  ChatGPT subscription (the mounted ``~/.codex/auth.json``) or an OpenAI API key.
* ``ApiBackend`` goes through SREGym's LiteLLM backend (``AGENT_MODEL_ID``,
  ``AGENT_API_BASE``, ``AGENT_API_KEY``) for people with provider keys.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from clients.harness.token_usage import token_count, usage_metrics

CLI_TIMEOUT_S = 300
DEFAULT_CODEX_HOME = "/root/.codex"
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "multi_agent",
    "plugins",
    "view_image",
    "image_generation",
    "browser_use",
    "computer_use",
    "skill_search",
    "workspace_dependencies",
)
# Popped so a mounted subscription login is used instead of API billing, as the judge bridge does.
API_BILLING_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE")


@dataclass
class StepResult:
    """What one model call produced."""

    parsed: dict | None
    raw_text: str
    usage: dict[str, int | None] = field(default_factory=usage_metrics)
    latency_s: float = 0.0
    error: str | None = None


class ModelBackend(Protocol):
    name: str
    model: str

    def version(self) -> str: ...

    def complete_json(self, prompt: str, schema: dict, *, step_dir: Path) -> StepResult: ...


def parse_codex_usage(events: list[dict]) -> dict[str, int | None]:
    """Token usage of one ``codex exec`` call, from its last ``turn.completed`` event."""
    usage = next((event.get("usage") for event in reversed(events) if event.get("type") == "turn.completed"), None)
    usage = usage if isinstance(usage, dict) else {}
    return usage_metrics(
        input_tokens=token_count(usage.get("input_tokens")),
        output_tokens=token_count(usage.get("output_tokens")),
        cached_input_tokens=token_count(usage.get("cached_input_tokens")),
        cache_creation_input_tokens=token_count(usage.get("cache_write_input_tokens")),
        reasoning_output_tokens=token_count(usage.get("reasoning_output_tokens")),
    )


def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _parse_answer(raw: str) -> tuple[dict | None, str | None]:
    if not raw.strip():
        return None, "empty model reply"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"reply is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "reply is not a JSON object"
    return parsed, None


class CodexExecBackend:
    name = "codex"

    def __init__(
        self,
        model: str,
        reasoning_effort: str | None = None,
        *,
        timeout_s: int = CLI_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ):
        # Provider prefixes such as openai/gpt-5.5 are LiteLLM conventions, not Codex ones.
        self.model = model.split("/")[-1]
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self._env = dict(os.environ if env is None else env)

    def environment(self) -> dict[str, str]:
        env = dict(self._env)
        home = Path(env.get("CODEX_HOME", DEFAULT_CODEX_HOME))
        if (home / "auth.json").exists():
            env["CODEX_HOME"] = str(home)
            for key in API_BILLING_VARS:
                env.pop(key, None)
        return env

    def version(self) -> str:
        result = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=30, env=self.environment()
        )
        return (result.stdout or result.stderr).strip()

    def command(self, schema_path: Path, answer_path: Path) -> list[str]:
        from clients.codex.codex_agent import custom_provider_args

        features = ",".join(f"{key}=false" for key in CODEX_DISABLED_FEATURES)
        command = [
            "codex",
            "-a",
            "never",
            "exec",
            "--model",
            self.model,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            'web_search="disabled"',
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            f"features={{{features}}}",
        ]
        if self.reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{self.reasoning_effort}"'])
        command.extend(custom_provider_args(self._env))
        command.extend(["--json", "--output-schema", str(schema_path), "--output-last-message", str(answer_path), "-"])
        return command

    def complete_json(self, prompt: str, schema: dict, *, step_dir: Path) -> StepResult:
        step_dir.mkdir(parents=True, exist_ok=True)
        schema_path = step_dir / "schema.json"
        answer_path = step_dir / "answer.json"
        (step_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        answer_path.unlink(missing_ok=True)
        command = self.command(schema_path, answer_path)
        started = time.monotonic()
        error: str | None = None
        with (
            (step_dir / "events.jsonl").open("w") as events_out,
            (step_dir / "stderr.log").open("w") as stderr_out,
            subprocess.Popen(
                command,
                cwd=step_dir,
                env=self.environment(),
                stdin=subprocess.PIPE,
                stdout=events_out,
                stderr=stderr_out,
                text=True,
                start_new_session=True,
            ) as process,
        ):
            try:
                process.communicate(prompt, timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                error = f"codex exec timed out after {self.timeout_s}s"
            returncode = process.returncode
        latency = round(time.monotonic() - started, 3)
        events = read_events(step_dir / "events.jsonl")
        usage = parse_codex_usage(events)
        failed = next((event for event in events if event.get("type") == "turn.failed"), None)
        if error is None and failed is not None:
            error = f"codex turn failed: {json.dumps(failed)[:500]}"
        if error is None and returncode:
            stderr_tail = (step_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")[-500:]
            error = f"codex exec exited {returncode}: {stderr_tail.strip()}"
        raw = answer_path.read_text(encoding="utf-8", errors="replace") if answer_path.exists() else ""
        parsed, parse_error = _parse_answer(raw) if error is None else (None, None)
        return StepResult(parsed=parsed, raw_text=raw, usage=usage, latency_s=latency, error=error or parse_error)


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text") is not None:
                parts.append(str(block["text"]))
        return "".join(parts)
    return str(content or "")


def extract_json_object(text: str) -> str:
    """The outermost ``{...}`` in a reply, with markdown fences stripped."""
    clean = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    start, end = clean.find("{"), clean.rfind("}")
    return clean[start : end + 1] if start != -1 and end > start else clean


class ApiBackend:
    """Provider-key path through SREGym's LiteLLM wrapper."""

    name = "api"

    def __init__(self, model: str, reasoning_effort: str | None = None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._backend = None

    def _get(self):
        if self._backend is None:
            from llm_backend.init_backend import get_llm_backend_for_agent

            self._backend = get_llm_backend_for_agent()
        return self._backend

    def version(self) -> str:
        return "litellm"

    def complete_json(self, prompt: str, schema: dict, *, step_dir: Path) -> StepResult:
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (step_dir / "schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        system = (
            "You are an SRE agent. Reply with exactly one JSON object matching this JSON schema and nothing else:\n"
        )
        system += json.dumps(schema)
        started = time.monotonic()
        try:
            response = self._get().inference(prompt, system_prompt=system)
        except Exception as exc:
            return StepResult(
                None, "", latency_s=round(time.monotonic() - started, 3), error=f"model call failed: {exc}"
            )
        latency = round(time.monotonic() - started, 3)
        raw = content_text(getattr(response, "content", response))
        (step_dir / "answer.json").write_text(raw, encoding="utf-8")
        meta = getattr(response, "usage_metadata", None) or {}
        input_details = meta.get("input_token_details") or {}
        output_details = meta.get("output_token_details") or {}
        usage = usage_metrics(
            input_tokens=token_count(meta.get("input_tokens")),
            output_tokens=token_count(meta.get("output_tokens")),
            cached_input_tokens=token_count(input_details.get("cache_read")),
            cache_creation_input_tokens=token_count(input_details.get("cache_creation")),
            reasoning_output_tokens=token_count(output_details.get("reasoning")),
        )
        parsed, error = _parse_answer(extract_json_object(raw))
        return StepResult(parsed=parsed, raw_text=raw, usage=usage, latency_s=latency, error=error)
