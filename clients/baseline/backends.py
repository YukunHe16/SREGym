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
    reasoning: str | None = None  # the provider's visible reasoning, for session mode to pass back


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


DEFAULT_API_MAX_TOKENS = 32768
API_RETRY_WAIT_S = 5
# Rate limits (HTTP 429, e.g. a subscription plan's per-window quota) are waited out rather than
# treated as a failed step: up to API_RATE_LIMIT_RETRIES extra attempts, API_RATE_LIMIT_WAIT_S apart.
API_RATE_LIMIT_RETRIES = 6
API_RATE_LIMIT_WAIT_S = 60


def is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "429" in text or "too many requests" in text


def parse_api_usage(usage: object) -> dict[str, int | None]:
    """Map an OpenAI-style usage block (LiteLLM or raw) to SREGym's token metrics."""

    def get(obj, key):
        if obj is None:
            return None
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    prompt_details = get(usage, "prompt_tokens_details")
    completion_details = get(usage, "completion_tokens_details")
    return usage_metrics(
        input_tokens=token_count(get(usage, "prompt_tokens")),
        output_tokens=token_count(get(usage, "completion_tokens")),
        cached_input_tokens=token_count(get(prompt_details, "cached_tokens")),
        reasoning_output_tokens=token_count(get(completion_details, "reasoning_tokens")),
    )


@dataclass
class Reply:
    """What one completion returned, whichever way it was transported."""

    content: str = ""
    reasoning: str | None = None
    usage: object = None
    finish_reason: str | None = None


def _reasoning_of(message: object) -> str | None:
    """The provider's visible reasoning on a message or a stream delta, if any."""
    reasoning = getattr(message, "reasoning_content", None)
    if not reasoning:
        fields = getattr(message, "provider_specific_fields", None)
        reasoning = fields.get("reasoning_content") if isinstance(fields, dict) else None
    return str(reasoning) if reasoning else None


class ApiBackend:
    """Provider-key path: one LiteLLM chat completion per step.

    Reads ``AGENT_MODEL_ID`` (LiteLLM model string, e.g. ``openai/glm-5.3`` for an
    OpenAI-compatible endpoint), ``AGENT_API_BASE`` and ``AGENT_API_KEY``. Extra
    request fields such as a provider's thinking switch come from
    ``BASELINE_EXTRA_BODY`` (JSON); ``AGENT_REASONING_EFFORT`` is forwarded as
    ``reasoning_effort`` when set.
    """

    name = "api"

    def __init__(self, model: str, reasoning_effort: str | None = None, *, env: dict[str, str] | None = None):
        source = os.environ if env is None else env
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api_base = source.get("AGENT_API_BASE") or None
        self.api_key = source.get("AGENT_API_KEY") or None
        self.max_tokens = int(source.get("BASELINE_MAX_TOKENS", DEFAULT_API_MAX_TOKENS))
        self.extra_body = json.loads(source.get("BASELINE_EXTRA_BODY") or "{}")
        self.timeout_s = int(source.get("BASELINE_API_TIMEOUT", "600"))
        # Wall-clock bound on one call, enforced by the driver itself: a provider that keeps the
        # connection open without sending bytes never trips the HTTP read timeout.
        self.hard_deadline_s = float(source.get("BASELINE_API_HARD_DEADLINE_S", str(self.timeout_s + 60)))
        # Streaming keeps bytes flowing while the model thinks: SREGym's egress proxy drops a
        # connection that stays silent for 10 minutes, which a long non-streamed reply can exceed.
        self.stream = source.get("BASELINE_API_STREAM", "1") != "0"

    def version(self) -> str:
        from importlib.metadata import version

        return f"litellm {version('litellm')}"

    def system_message(self, schema: dict) -> dict:
        return {
            "role": "system",
            "content": (
                "You are an SRE agent. Reply with exactly one JSON object matching this JSON schema and nothing else, "
                "no markdown fences and no commentary:\n" + json.dumps(schema)
            ),
        }

    def build_request(self, messages: list[dict]) -> dict:
        request = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout_s,
        }
        if self.api_base:
            request["api_base"] = self.api_base
        if self.api_key:
            request["api_key"] = self.api_key
        extra = dict(self.extra_body)
        if self.reasoning_effort:
            extra["reasoning_effort"] = self.reasoning_effort
        if extra:
            request["extra_body"] = extra
        return request

    def request(self, prompt: str, schema: dict) -> dict:
        return self.build_request([self.system_message(schema), {"role": "user", "content": prompt}])

    def complete_json(self, prompt: str, schema: dict, *, step_dir: Path) -> StepResult:
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (step_dir / "schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        return self._call(self.request(prompt, schema), step_dir)

    def complete_messages(self, messages: list[dict], *, step_dir: Path) -> StepResult:
        """Session mode: the driver owns the conversation and this sends it unchanged."""
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(
            json.dumps(messages, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        (step_dir / "prompt.txt").write_text(str(last_user), encoding="utf-8")
        return self._call(self.build_request(messages), step_dir)

    def _completion_with_deadline(self, request: dict) -> Reply:
        """One completion, streamed or not, that gives up at the wall-clock deadline."""
        if self.stream:
            return self._stream(request)
        import threading

        import litellm

        box: dict = {}

        def work() -> None:
            try:
                box["response"] = litellm.completion(**request)
            except Exception as exc:  # reported by the caller
                box["exception"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(self.hard_deadline_s)
        if worker.is_alive():  # the thread dies with the process
            raise TimeoutError(f"no reply within the hard deadline of {self.hard_deadline_s:.0f}s")
        if "exception" in box:
            raise box["exception"]
        response = box["response"]
        message = response.choices[0].message
        return Reply(
            content=message.content or "",
            reasoning=_reasoning_of(message),
            usage=getattr(response, "usage", None),
            finish_reason=getattr(response.choices[0], "finish_reason", None),
        )

    def _stream(self, request: dict) -> Reply:
        """Accumulate a streamed reply; the deadline is checked between chunks and the stream closed on expiry."""
        import litellm

        deadline = time.monotonic() + self.hard_deadline_s
        reply = Reply()
        stream = litellm.completion(**request, stream=True, stream_options={"include_usage": True})
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"reply still streaming at the hard deadline of {self.hard_deadline_s:.0f}s")
                if getattr(chunk, "usage", None):
                    reply.usage = chunk.usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                if getattr(delta, "content", None):
                    reply.content += delta.content
                reasoning = _reasoning_of(delta)
                if reasoning:
                    reply.reasoning = (reply.reasoning or "") + reasoning
                if getattr(choices[0], "finish_reason", None):
                    reply.finish_reason = choices[0].finish_reason
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        return reply

    def _call(self, request: dict, step_dir: Path) -> StepResult:
        started = time.monotonic()
        reply: Reply | None = None
        error: str | None = None
        plain_attempts = 0
        rate_limit_attempts = 0
        while True:
            try:
                reply = self._completion_with_deadline(request)
                error = None
                break
            except Exception as exc:  # provider or transport error; the driver decides what a failed step means
                error = f"model call failed: {type(exc).__name__}: {str(exc)[:300]}"
                if is_rate_limit(exc) and rate_limit_attempts < API_RATE_LIMIT_RETRIES:
                    rate_limit_attempts += 1
                    time.sleep(API_RATE_LIMIT_WAIT_S)
                    continue
                plain_attempts += 1
                if plain_attempts >= 2:
                    break
                time.sleep(API_RETRY_WAIT_S)
        latency = round(time.monotonic() - started, 3)
        if reply is None:
            (step_dir / "stderr.log").write_text(error or "", encoding="utf-8")
            return StepResult(None, "", latency_s=latency, error=error)
        raw = reply.content
        (step_dir / "answer.json").write_text(raw, encoding="utf-8")
        if reply.reasoning:
            (step_dir / "reasoning.txt").write_text(reply.reasoning, encoding="utf-8")
        usage = parse_api_usage(reply.usage)
        parsed, parse_error = _parse_answer(extract_json_object(raw))
        if parse_error and reply.finish_reason == "length":
            parse_error = f"{parse_error} (output truncated at max_tokens={self.max_tokens})"
        return StepResult(
            parsed=parsed, raw_text=raw, usage=usage, latency_s=latency, error=parse_error, reasoning=reply.reasoning
        )
