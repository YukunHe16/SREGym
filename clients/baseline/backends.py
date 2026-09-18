"""The model side of the baseline agent: one LiteLLM chat completion per step, with tools.

Provider-key models only (``AGENT_MODEL_ID`` as a LiteLLM model string, ``AGENT_API_BASE``,
``AGENT_API_KEY``). Replies are streamed by default so that a long think keeps bytes flowing
(SREGym's egress proxy drops a connection that stays silent for ten minutes) and reassembled
into content, reasoning and tool calls. Every call has a wall-clock deadline; rate limits are
waited out rather than treated as a failed step.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from importlib.metadata import version as package_version
from pathlib import Path

from clients.baseline.tools import ToolSpec
from clients.harness.token_usage import token_count, usage_metrics

DEFAULT_API_MAX_TOKENS = 65536
API_RETRY_WAIT_S = 5
API_RATE_LIMIT_RETRIES = 6
API_RATE_LIMIT_WAIT_S = 60
# A gateway or DNS blip on the way out of the container is not the model's answer: wait it out.
API_TRANSIENT_RETRIES = 5
API_TRANSIENT_WAIT_S = 30


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as the model produced it


@dataclass
class Reply:
    """What one model call produced."""

    content: str = ""
    reasoning: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=usage_metrics)
    finish_reason: str | None = None
    latency_s: float = 0.0
    error: str | None = None
    content_from_reasoning: bool = False

    def as_message(self, *, pass_reasoning: bool) -> dict:
        """The assistant message to append to the conversation."""
        message: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        if pass_reasoning and self.reasoning and not self.content_from_reasoning:
            message["reasoning_content"] = self.reasoning
        return message


def is_rate_limit(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "429" in text or "too many requests" in text


_TRANSIENT = (
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "no address associated with hostname",
    "temporary failure in name resolution",
    "connection reset",
    "connection refused",
    "connection error",
    "apiconnectionerror",
    "internalservererror",
    " 502",
    " 503",
    " 504",
)


def is_transient(exc: Exception) -> bool:
    """A failure of the path to the provider rather than of the request."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(needle in text for needle in _TRANSIENT)


# Valid JSON escapes are consumed as pairs so they stay intact; any other backslash is a stray one.
_ESCAPE_OR_STRAY = re.compile(r'\\["\\/bfnrtu]|\\')


def repair_json_text(raw: str) -> str:
    """Double stray backslashes so that a shell-command argument with ``\\d`` or ``\\n`` still parses."""
    return _ESCAPE_OR_STRAY.sub(lambda m: m.group(0) if len(m.group(0)) == 2 else "\\\\", raw)


def parse_arguments(raw: str) -> tuple[dict | None, str | None]:
    """Tool-call arguments as a dict, or an error message."""
    text = (raw or "").strip() or "{}"
    for candidate in (text, repair_json_text(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed, None
        return None, "tool arguments are not a JSON object"
    return None, f"tool arguments are not valid JSON: {text[:120]}"


def parse_api_usage(usage: object) -> dict:
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


def _reasoning_of(message: object) -> str | None:
    reasoning = getattr(message, "reasoning_content", None)
    if not reasoning:
        fields = getattr(message, "provider_specific_fields", None)
        reasoning = fields.get("reasoning_content") if isinstance(fields, dict) else None
    return str(reasoning) if reasoning else None


def _reply_from_response(response) -> Reply:
    choice = response.choices[0]
    message = choice.message
    calls = []
    for tc in getattr(message, "tool_calls", None) or []:
        function = getattr(tc, "function", None)
        calls.append(
            ToolCall(
                str(getattr(tc, "id", "") or f"call_{len(calls)}"),
                str(getattr(function, "name", "") or ""),
                str(getattr(function, "arguments", "") or ""),
            )
        )
    return Reply(
        content=message.content or "",
        reasoning=_reasoning_of(message),
        tool_calls=calls,
        usage=parse_api_usage(getattr(response, "usage", None)),
        finish_reason=getattr(choice, "finish_reason", None),
    )


class ApiBackend:
    """One LiteLLM chat completion per step, with tools."""

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
        # Wall-clock bound on one call, enforced here: a provider that keeps the connection open
        # without sending bytes never trips the HTTP read timeout.
        self.hard_deadline_s = float(source.get("BASELINE_API_HARD_DEADLINE_S", str(self.timeout_s + 60)))
        self.stream = source.get("BASELINE_API_STREAM", "1") != "0"
        # Sampling is left at the provider's default, as the CLI agents do; set BASELINE_TEMPERATURE to pin it.
        raw_temperature = source.get("BASELINE_TEMPERATURE")
        self.temperature = float(raw_temperature) if raw_temperature not in (None, "") else None

    def version(self) -> str:
        return f"litellm {package_version('litellm')}"

    @staticmethod
    def system_message(text: str) -> dict:
        return {"role": "system", "content": text}

    def build_request(self, messages: list[dict], tools: list[ToolSpec]) -> dict:
        request: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout_s,
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if tools:
            request["tools"] = [spec.as_openai() for spec in tools]
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

    def complete(self, messages: list[dict], tools: list[ToolSpec], *, step_dir: Path) -> Reply:
        """Send the conversation with the tool list; record what was sent and what came back."""
        step_dir.mkdir(parents=True, exist_ok=True)
        (step_dir / "messages.json").write_text(
            json.dumps(messages, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (step_dir / "tools.json").write_text(json.dumps([s.name for s in tools]) + "\n", encoding="utf-8")
        request = self.build_request(messages, tools)
        started = time.monotonic()
        reply: Reply | None = None
        error: str | None = None
        plain_attempts = 0
        rate_limit_attempts = 0
        transient_attempts = 0
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
                if is_transient(exc) and transient_attempts < API_TRANSIENT_RETRIES:
                    transient_attempts += 1
                    time.sleep(API_TRANSIENT_WAIT_S)
                    continue
                plain_attempts += 1
                if plain_attempts >= 2:
                    break
                time.sleep(API_RETRY_WAIT_S)
        latency = round(time.monotonic() - started, 3)
        if reply is None:
            (step_dir / "stderr.log").write_text(error or "", encoding="utf-8")
            return Reply(latency_s=latency, error=error)
        reply.latency_s = latency
        (step_dir / "answer.json").write_text(
            json.dumps(
                {
                    "content": reply.content,
                    "tool_calls": [tc.__dict__ for tc in reply.tool_calls],
                    "finish_reason": reply.finish_reason,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        if reply.reasoning:
            (step_dir / "reasoning.txt").write_text(reply.reasoning, encoding="utf-8")
        if not reply.content and not reply.tool_calls and reply.reasoning and reply.finish_reason != "length":
            # Thinking models sometimes put the whole reply, action block and all, in
            # reasoning_content and leave content empty. Read the reasoning as the reply
            # rather than losing the step; a reasoning text with no usable action still
            # goes through the protocol's own format handling.
            reply.content = reply.reasoning
            reply.content_from_reasoning = True
        if not reply.content and not reply.tool_calls:
            reply.error = "empty model reply" + (
                f" (output truncated at max_tokens={self.max_tokens})" if reply.finish_reason == "length" else ""
            )
        return reply

    def _completion_with_deadline(self, request: dict) -> Reply:
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
        return _reply_from_response(box["response"])

    def _stream(self, request: dict) -> Reply:
        """Collect the streamed chunks (deadline checked between chunks) and rebuild the reply."""
        import litellm

        deadline = time.monotonic() + self.hard_deadline_s
        chunks = []
        stream = litellm.completion(**request, stream=True, stream_options={"include_usage": True})
        try:
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"reply still streaming at the hard deadline of {self.hard_deadline_s:.0f}s")
                chunks.append(chunk)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()
        if not chunks:
            raise RuntimeError("empty stream")
        response = litellm.stream_chunk_builder(chunks, messages=request["messages"])
        reply = _reply_from_response(response)
        if reply.reasoning is None:  # the builder may drop provider fields; take them from the deltas
            pieces = []
            for chunk in chunks:
                for choice in getattr(chunk, "choices", None) or []:
                    text = _reasoning_of(getattr(choice, "delta", None))
                    if text:
                        pieces.append(text)
            reply.reasoning = "".join(pieces) or None
        if reply.usage.get("input_tokens") is None:
            usage = next((getattr(c, "usage", None) for c in reversed(chunks) if getattr(c, "usage", None)), None)
            reply.usage = parse_api_usage(usage)
        return reply
