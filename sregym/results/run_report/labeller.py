"""The small model that answers yes/no and pick-one questions about pieces of a run.

Every question is either
    {"type": "noul",   "instructions": <text or object>}                      -> {"noul": 0..1}
    {"type": "choice", "instructions": ..., "criteria": {option: description}} -> {"choice": option, "probabilities": {...}}
    {"type": "quote",  "instructions": ...}                                    -> {"quote": words copied from the state}
    {"type": "explain", "instructions": ...}                                   -> {"text": its own short explanation}
A quote is asked only of a back end that can answer one (``Labeller.quotes``: a chat model through LiteLLM, not Jev);
the caller checks that the words are in what it showed, so a quote is evidence a reader can find, not a paraphrase.
An explanation is asked of the same back ends; it is the model's own words, shown as such.
A request is one shared ``state`` plus up to 16 questions. Two back ends answer the same questions:
Jev (TypeSafe) and any LiteLLM chat model, so the report does not depend on one vendor. Answers are cached by
content, which makes a re-run free and its numbers stable. Keys are never logged.

Requests are independent of each other, so several are out at once (``together``). What is asked does not depend on
that: the requests of a run are laid out before the first one is sent.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .masking import masked_all

MAX_QUESTIONS = 16  # the same limit as clients/jev/client.py



class LabellerError(RuntimeError):
    pass


class LabellerUnusable(LabellerError):
    """The back end turns every request away (no balance left on the account, a key it does not take): the run stops
    and says so, rather than write reports that lack all this labeller answers. What was answered stays cached, so a
    run after the account is put right asks only the rest (on 2026-09-23 a DeepSeek balance ran out in the middle of a
    suite, and every request after that failed without a word)."""


class CacheMiss(LabellerUnusable):
    """Offline replay is incomplete; stop rather than call a backend or report success."""


class LabellerRefused(LabellerError):
    """The model's own safeguards turned the request away (Claude Sonnet 5.5 on 2026-09-28: "safeguards flagged this
    message", the same 14 of 64 naming requests each time it was asked). The request's text is never changed to get it
    through; another model may be asked instead (``ClaudeCodeLabeller.FALLBACK``)."""


class Answers(dict):
    """A response, with the model that gave it where that is not the labeller's own model (``answered_by``)."""

    answered_by: str | None = None


def answered_by(answers: dict, model: str | None) -> dict:
    """``answers`` marked as given by ``model`` (none: left as they are)."""
    if not model:
        return answers
    marked = Answers(answers)
    marked.answered_by = model
    return marked


# what a provider says when the account, not the request, is the trouble; only a fixed reason is passed on, since
# the message may quote the request
RATE_LIMIT_WAITS = (5, 10, 20, 40, 60, 60)  # seconds waited before asking again after a rate limit
ACCOUNT_TROUBLE = re.compile(
    r"insufficient[ _]balance|insufficient_quota|exceeded your current quota|invalid[ _]api[ _]key|"
    r"incorrect api key|api key not valid",
    re.IGNORECASE,
)


class RequestTooLarge(LabellerError):
    """The request would exceed the back end's size limit; asking about fewer entries at once can help."""


class RefusedContent(LabellerError):
    """The back end's front door refused what the request carries: a firewall's page came back, not an answer (from
    the evening of 2026-09-22, TypeSafe's Cloudflare turns away some texts, such as an agent's note that mentions /etc/hosts). Asking
    about fewer entries at once leaves only the refused ones without an answer; their text is never changed."""


def read_key(name: str, key_file: Path | None) -> str | None:
    """An API key from the environment or from a KEY=value file (``export`` allowed); never printed."""
    if os.environ.get(name):
        return os.environ[name]
    if key_file and Path(key_file).exists():
        for line in Path(key_file).read_text().splitlines():
            key, found, value = line.strip().removeprefix("export ").partition("=")
            if found and key.strip() == name:
                return value.strip().strip('"').strip("'")
    return None


def _post(url: str, data: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    """POST ``data``; the status and the body of the reply. Through httpx, as SREGym's own Jev client sends
    (``clients/jev/client.py``): since the evening of 2026-09-22 TypeSafe's front end (Cloudflare, error 1010) turns away requests
    made with Python's urllib. Redirects are not followed, so the key goes to one host only."""
    import httpx

    response = httpx.post(url, content=data, headers=headers, timeout=timeout, follow_redirects=False)
    return response.status_code, response.content


def together(work, items: list, workers: int):
    """``work(item)`` for every item, up to ``workers`` at a time; yields the results in the order of the items."""
    if workers <= 1 or len(items) <= 1:
        yield from map(work, items)
        return
    pool = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    futures = [pool.submit(work, item) for item in items]
    try:
        for future in futures:
            yield future.result()
    except BaseException:
        # an unusable labeller (no balance, a key refused) or a stop: what is still queued is not sent (2026-09-28
        # review: every queued request had still gone out, each refused in turn)
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


class Labeller:
    name = "none"
    effort: str | None = None  # the reasoning effort asked of the model, for a back end that has such a thing
    quotes = False  # whether it answers questions of type "quote": words copied from the state

    def __init__(self, workers: int = 1) -> None:
        self.workers = max(1, workers)
        self.requests = 0
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.cache_hits = 0
        # Runs are built several at a time and each sends several requests at a time (``together``); a back end
        # sends while it holds a slot, so that never more than ``workers`` requests are out, whoever asks.
        self._slots = threading.BoundedSemaphore(self.workers)
        self._lock = threading.Lock()  # the counters, and the cache of a CachedLabeller

    def ask(self, state: dict, questions: dict) -> dict:
        raise NotImplementedError

    def again(self, state: dict, questions: dict, reading: int) -> dict:
        """Another reading of a request that was asked before. A model does not answer the same request the same
        way twice (Jev: 0.03 apart on average for answers in the middle), so a back end just answers afresh; the
        cache keeps the readings apart by their number."""
        return self.ask(state, questions)

    def stats(self) -> dict:
        return {
            "labeller": self.name,
            "workers": self.workers,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "estimated_cost_usd": None,
            "cache_hits": self.cache_hits,
        }


class JevLabeller(Labeller):
    """Jev through the question types and response validation SREGym already ships in ``clients/jev``."""

    def __init__(self, key: str, model: str = "jev-1.13.0", timeout: float = 60, workers: int = 1) -> None:
        super().__init__(workers)
        self._key, self.model, self.timeout = key, model, timeout
        self.name = model

    def ask(self, state: dict, questions: dict) -> dict:
        from clients.jev.client import MAX_REQUEST_BYTES, QUESTIONS

        state = masked_all(state)  # nothing leaves unmasked, cached or not

        try:
            typed = QUESTIONS.validate_python(questions)
        except ValueError as exc:
            raise LabellerError(f"invalid questions: {exc}") from None
        body = {
            "model": self.model,
            "state": state,
            "questions": {name: question.model_dump(exclude_none=True) for name, question in typed.items()},
        }
        data = json.dumps(body, allow_nan=False).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise RequestTooLarge(f"{len(data)} bytes, the limit is {MAX_REQUEST_BYTES}")
        with self._slots:  # held through the waits between attempts too: a server in trouble gets fewer requests
            return self._send(data, typed, questions)

    def _send(self, data: bytes, typed: dict, questions: dict) -> dict:
        import httpx

        from clients.jev.client import ENDPOINT, MAX_RESPONSE_BYTES, RETRY_STATUSES, validate_response

        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        error = "no attempt made"
        for attempt in range(4):
            try:
                status, body = _post(ENDPOINT, data, headers, self.timeout)
            except httpx.TimeoutException:
                # not retried: the provider may have answered, and billed, a request whose reply did not arrive
                raise LabellerError("timed out") from None
            except httpx.TransportError as exc:
                # a connection the server drops before it answers arrives as httpx.RemoteProtocolError
                error = type(exc).__name__
                time.sleep(1.5 * (attempt + 1))
                continue
            if status >= 300:
                error = f"HTTP {status}"
                if status in (401, 402):
                    raise LabellerUnusable(
                        f"{self.name}: {'the key is refused' if status == 401 else 'no balance left'}"
                    )
                if status in RETRY_STATUSES:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if status == 403 and body.lstrip()[:15].lower() == b"<!doctype html>":
                    raise RefusedContent(f"{error}: the service's firewall refused the content")
                raise LabellerError(error)
            try:
                raw = json.loads(body[:MAX_RESPONSE_BYTES])
            except ValueError:
                error = "the reply is not JSON"
                time.sleep(1.5 * (attempt + 1))
                continue
            # Usage returned alongside a malformed answer is still real usage.
            usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
            tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            with self._lock:
                self.requests += 1
                if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                    self.input_tokens += tokens
            try:
                clean = validate_response(raw, typed)
            except (ValueError, TypeError) as exc:
                raise LabellerError(f"invalid response: {exc}") from None
            return {name: _normalise(questions[name], answer) for name, answer in clean["answers"].items()}
        raise LabellerError(f"request failed after retries: {error}")

    def stats(self) -> dict:
        return {
            **super().stats(),
            "estimated_cost_usd": None,
            "price_source": None,
            "cached_input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
        }


class LiteLLMLabeller(Labeller):
    """Any chat model through LiteLLM. Slower and dearer than Jev, but has no vendor lock-in."""

    PROMPT = (
        "You label pieces of an SRE agent's run. Answer every question about `state`. Reply with ONE JSON object "
        "and nothing else. For a question of type `noul` give a number from 0 to 1: how true the statement is. "
        "For a question of type `choice` give exactly one of the option names listed in its `criteria`. "
        "For a question of type `quote` give a string: the words it asks for, copied exactly as they stand in "
        '`state` (one line or sentence, not joined from several places, nothing added), or "none". '
        "For a question of type `explain` give a string: a short explanation in your own words, as the question asks."
    )
    quotes = True

    def __init__(
        self,
        model: str,
        api_base: str | None = None,
        api_key: str | None = None,
        effort: str | None = None,
        workers: int = 1,
    ) -> None:
        super().__init__(workers)
        self.model, self.api_base, self.api_key = model, api_base, api_key
        # A reasoning model reads differently at another effort, so an effort that was set is part of the name: of
        # what the report says answered, and of the cache key. Not set, nothing is sent and the provider's default
        # applies (deepseek-flash reasons by default: about 850 output tokens per request of one entry).
        self._effort = effort
        self.effort = effort or "provider default"
        self.name = f"litellm:{model}" + (f" effort={effort}" if effort else "")
        self.output_tokens = 0
        self.reasoning_tokens = 0
        # LiteLLM logs two lines per request at INFO, and reading the problem definitions sets the root logger there
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)

    def ask(self, state: dict, questions: dict) -> dict:
        import litellm  # imported late so the rest of the tool needs only the standard library

        state = masked_all(state)  # nothing leaves unmasked, cached or not
        user = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        kwargs = {"api_base": self.api_base, "api_key": self.api_key, "reasoning_effort": self._effort}
        if self._effort:  # without this LiteLLM refuses the parameter for a model it does not know
            kwargs["allowed_openai_params"] = ["reasoning_effort"]
        error = "no attempt made"
        waits = iter(RATE_LIMIT_WAITS)
        attempt = 0
        while attempt < 2:  # a reply that cannot be read is asked for once more (1 of 726 requests in a real run)
            attempt += 1
            try:
                with self._slots:
                    response = litellm.completion(
                        model=self.model,
                        messages=[{"role": "system", "content": self.PROMPT}, {"role": "user", "content": user}],
                        num_retries=2,  # LiteLLM's own, for what is worth trying again: rate limits, dropped connections
                        **{k: v for k, v in kwargs.items() if v},
                    )
            except Exception as exc:  # what a provider raises differs by vendor; the message may quote the request
                if type(exc).__name__ == "RateLimitError" and not ACCOUNT_TROUBLE.search(str(exc)):
                    # a limit on requests at once or per minute (GLM's subscription allows about five at once):
                    # LiteLLM's quick retries meet it again, so wait, and then ask again without counting it
                    wait = next(waits, None)
                    if wait is not None:
                        time.sleep(wait)
                        attempt -= 1
                        continue
                if "ContextWindow" in type(exc).__name__:
                    raise RequestTooLarge(type(exc).__name__) from None
                trouble = ACCOUNT_TROUBLE.search(str(exc))
                if trouble or type(exc).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                    why = (
                        "no balance left" if trouble and "key" not in trouble.group(0).lower() else "the key is refused"
                    )
                    raise LabellerUnusable(f"{self.name}: {why}") from None
                raise LabellerError(type(exc).__name__) from None
            usage = getattr(response, "usage", None)
            details = getattr(usage, "completion_tokens_details", None)
            with self._lock:
                self.requests += 1
                self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                self.cached_input_tokens += int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
                self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                self.reasoning_tokens += int(getattr(details, "reasoning_tokens", 0) or 0)
            try:
                return self._read(response.choices[0].message.content or "", questions)
            except LabellerError as exc:
                error = str(exc)
        raise LabellerError(error)

    def stats(self) -> dict:
        return {
            **super().stats(),
            "effort": self.effort,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,  # part of the output tokens, where the provider reports them
        }

    @staticmethod
    def _read(text: str, questions: dict) -> dict:
        start, end = text.find("{"), text.rfind("}")
        try:
            raw = json.loads(text[start : end + 1])
        except ValueError:
            raise LabellerError("the model did not return JSON") from None
        if not isinstance(raw, dict):
            raise LabellerError("the model did not return a JSON object")
        out = {}
        for name, question in questions.items():
            value = raw.get(name)
            if isinstance(value, dict):  # a model that copies Jev's shape: {"noul": ...}, {"choice": ..., ...}
                value = value.get({"noul": "noul", "quote": "quote", "explain": "text"}.get(question["type"], "choice"))
            if question["type"] == "explain":
                if not isinstance(value, str) or not value.strip():
                    raise LabellerError(f"no explanation for {name}")
                out[name] = {"text": value.strip()}
            elif question["type"] == "quote":
                if not isinstance(value, str):
                    raise LabellerError(f"no words for {name}")
                out[name] = {"quote": value.strip()}
            elif question["type"] == "noul":
                try:
                    out[name] = {"noul": min(1.0, max(0.0, float(value)))}
                except (TypeError, ValueError):
                    raise LabellerError(f"no number for {name}") from None
            else:
                # anything else that is not an option name is a reply that cannot be read, asked for once more, not
                # an error that stops the run (an object in its place once did: TypeError, unhashable dict)
                if not isinstance(value, str) or value not in question["criteria"]:
                    raise LabellerError(f"{name}: {value!r} is not one of the options")
                out[name] = {"choice": value, "probabilities": {k: float(k == value) for k in question["criteria"]}}
        return out


def _normalise(question: dict, answer: dict) -> dict:
    if question["type"] == "noul":
        return {"noul": float(answer["noul"])}
    if question["type"] == "quote":
        return {"quote": str(answer["quote"])}
    if question["type"] == "explain":
        return {"text": str(answer["text"])}
    return {"choice": answer["choice"], "probabilities": answer.get("probabilities") or {}}


class CodexLabeller(LiteLLMLabeller):
    """A model of a ChatGPT subscription, through the Codex CLI (``codex exec``), with the subscription's own login:
    no API key, no bill per token, but the subscription's usage limits. Each request runs Codex once, in an empty
    folder, read-only, told to run nothing and to answer from the request alone; its last message is the answer, read
    as the chat labeller's (2026-09-28: DeepSeek's balance ran out; the user chose gpt-6-luna on the subscription)."""

    NOTE = (
        " Everything you need is in the request below: do not run any command, open any file or search the web."
        " Your final message must be that JSON object alone."
    )
    TIMEOUT = 900  # seconds per request
    # what the Codex CLI's session variables of a Claude session, and an OpenAI key, must not reach: the subscription
    # login is used, never a key that would bill (see memory: desktop session variables leak into child processes)
    DROPPED = re.compile(r"^(ANTHROPIC_.*|CLAUDE.*|MCP_.*|OPENAI_API_KEY|OPENAI_BASE_URL)$")

    def __init__(self, model: str, effort: str | None = None, workers: int = 1) -> None:
        Labeller.__init__(self, workers)
        self.model, self._effort = model, effort or "medium"
        self.effort = self._effort
        self.name = f"codex:{model} effort={self._effort}"
        self.output_tokens = self.reasoning_tokens = 0

    def ask(self, state: dict, questions: dict) -> dict:
        import subprocess
        import tempfile

        state = masked_all(state)  # nothing leaves unmasked, cached or not
        prompt = self.PROMPT + self.NOTE + "\n\n" + json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        env = {k: v for k, v in os.environ.items() if not self.DROPPED.match(k)}
        error = "no attempt made"
        for _ in range(2):  # a reply that cannot be read is asked for once more
            with self._slots, tempfile.TemporaryDirectory(prefix="run-report-codex-") as folder:
                last = Path(folder) / "last.txt"
                command = ["codex", "exec", "-m", self.model, "-c", f'model_reasoning_effort="{self._effort}"',
                           "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-C", folder, "-o", str(last), "-"]  # fmt: skip
                try:
                    done = subprocess.run(command, input=prompt, capture_output=True, text=True, env=env,
                                          cwd=folder, timeout=self.TIMEOUT)  # fmt: skip
                except subprocess.TimeoutExpired:
                    raise LabellerError("codex timed out") from None
                except OSError as exc:
                    raise LabellerUnusable(f"{self.name}: codex could not be started ({type(exc).__name__})") from None
                said = done.stdout + done.stderr
                used = re.search(r"tokens used\s*\n\s*([\d,]+)", said)
                with self._lock:
                    self.requests += 1
                    self.input_tokens += 0  # the CLI reports one total; it is kept as output below, marked as such
                    self.output_tokens += int(used.group(1).replace(",", "")) if used else 0
                if re.search(r"usage limit|rate limit reached|log ?in|not logged in|unauthori[sz]ed", said, re.I) and done.returncode:
                    raise LabellerUnusable(f"{self.name}: the subscription refused the request (usage limit or login)")
                if done.returncode:
                    raise LabellerError(f"codex exited {done.returncode}")
                text = last.read_text(encoding="utf-8", errors="replace") if last.exists() else ""
            try:
                return self._read(text, questions)
            except LabellerError as exc:
                error = str(exc)
        raise LabellerError(error)

    def stats(self) -> dict:
        return {**super().stats(), "tokens_note": "output_tokens holds the CLI's total per request (input and output)"}


class ClaudeCodeLabeller(LiteLLMLabeller):
    """A Claude model of a Claude subscription, through Claude Code in print mode (``claude -p``) with the
    subscription's token (``claude setup-token``): no API key, the subscription's usage limits. No tools, no settings,
    no MCP servers, nothing kept; the process gets only HOME, PATH and the token, so that neither a key nor the
    variables of the Claude session this tool may run in reach it."""

    NOTE = CodexLabeller.NOTE
    # what Claude Code prints when the model's safeguards refuse a request, and the model asked instead then
    REFUSED = re.compile(r"safeguards flagged this message", re.I)
    FALLBACK = "claude-sonnet-5"  # Opus 5.5 refused the same requests as Sonnet 5.5; Sonnet 5 answered them

    def __init__(self, model: str, token: str, effort: str | None = None, workers: int = 1) -> None:
        Labeller.__init__(self, workers)
        self.model, self._token, self._effort = model, token, effort
        self.effort = effort or "default"
        self.name = f"claudecode:{model}" + (f" effort={effort}" if effort else "")
        self.output_tokens = self.reasoning_tokens = 0
        self.fallback = None if model == self.FALLBACK else self.FALLBACK
        self.refused = self.answered_by_fallback = 0

    @staticmethod
    def binary() -> str:
        """``claude`` on PATH, else the newest one the Claude desktop app keeps (``CLAUDE_BIN`` names another)."""
        import shutil

        if os.environ.get("CLAUDE_BIN"):
            return os.environ["CLAUDE_BIN"]
        found = shutil.which("claude")
        if found:
            return found
        bundled = sorted(
            (Path.home() / "Library/Application Support/Claude/claude-code").glob("*/claude.app/Contents/MacOS/claude"),
            key=lambda path: [int(x) if x.isdigit() else x for x in re.split(r"[.]", path.parents[3].name)],
        )
        if not bundled:
            raise LabellerUnusable("claudecode: no claude found (PATH, CLAUDE_BIN, or the Claude app's own)")
        return str(bundled[-1])

    def ask(self, state: dict, questions: dict) -> dict:
        state = masked_all(state)
        prompt = self.PROMPT + self.NOTE + "\n\n" + json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        try:
            return self._answer(self.model, prompt, questions)
        except LabellerRefused:
            with self._lock:
                self.refused += 1
            if not self.fallback:
                raise
        # the same request, word for word, to the other model (the user's choice, 2026-09-28); its refusal stands
        answers = answered_by(self._answer(self.fallback, prompt, questions), self.fallback)
        with self._lock:
            self.answered_by_fallback += 1
        return answers

    def _answer(self, model: str, prompt: str, questions: dict) -> dict:
        import subprocess
        import tempfile

        env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": self._token}
        command = [self.binary(), "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                   "--no-session-persistence", "--output-format", "json"] + (["--effort", self._effort] if self._effort else [])  # fmt: skip
        error = "no attempt made"
        for _ in range(2):
            with self._slots, tempfile.TemporaryDirectory(prefix="run-report-claude-") as folder:
                try:
                    done = subprocess.run(command, input=prompt, capture_output=True, text=True, env=env, cwd=folder,
                                          timeout=CodexLabeller.TIMEOUT)  # fmt: skip
                except subprocess.TimeoutExpired:
                    raise LabellerError("claude timed out") from None
                except OSError as exc:
                    raise LabellerUnusable(f"{self.name}: claude could not be started ({type(exc).__name__})") from None
            try:
                reply = json.loads(done.stdout or "{}")
            except ValueError:
                reply = {}
            usage = reply.get("usage") or {}
            with self._lock:
                self.requests += 1
                self.input_tokens += int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                self.cached_input_tokens += int(usage.get("cache_read_input_tokens") or 0)
                self.output_tokens += int(usage.get("output_tokens") or 0)
            text = str(reply.get("result") or "")
            if reply.get("is_error") or done.returncode:
                if self.REFUSED.search(text + done.stderr):  # the same text is refused again: not retried
                    raise LabellerRefused(f"{model}: its safeguards refused the request")
                if re.search(r"usage limit|rate limit|limit reached|invalid.*token|unauthori[sz]ed|log ?in", text + done.stderr, re.I):
                    raise LabellerUnusable(f"{self.name}: the subscription refused the request (usage limit or token)")
                raise LabellerError(f"claude exited {done.returncode}")
            try:
                return self._read(text, questions)
            except LabellerError as exc:
                error = str(exc)
        raise LabellerError(error)

    def stats(self) -> dict:
        return {**super().stats(), "refused_by_safeguards": self.refused, "fallback_model": self.fallback,
                "answered_by_fallback": self.answered_by_fallback}


class CachedLabeller(Labeller):
    """Version 2 caches whole responses, keyed by the whole request and the labeller's identity.

    Rows of the old cache (one per question) still answer a request the way the old tool did: only when every
    question of it is there for the same state, each with the answer written last. That can mix two requests'
    answers where a question was asked again beside another wording (2 of the 1,288 rows of the gpt-6-luna reports
    of 2026-09-25 were written twice); it is what the old reports print, and such requests are counted
    (``legacy_requests_with_rewritten_rows``). Such an answer is written again as a version 2 row marked
    ``from_legacy``, so the reports built before 2026-09-26 are rebuilt as they were without asking again.
    A file with old rows asks nothing new unless the caller opts in (``allow_new_requests``); ``cache_only`` asks
    nothing at all.
    """

    def __init__(self, inner: Labeller, cache_path: Path, also_read: tuple[Path, ...] = (),
                 cache_only: bool = False, allow_new_requests: bool = False, request_identity: dict | None = None) -> None:
        super().__init__(inner.workers)
        self.inner, self.path, self.name, self.effort = inner, Path(cache_path), inner.name, inner.effort
        self.quotes = inner.quotes
        self._cache: dict[str, dict] = {}
        self._asking: dict[str, threading.Event] = {}
        self.legacy_rows = 0
        self.legacy_used = 0
        self._legacy: dict[str, dict] = {}
        self._legacy_twice: set[str] = set()  # keys answered more than once, differently
        self.legacy_rewritten = 0
        self.request_identity = request_identity or self.identity(inner)
        self.progress = None
        for path in [Path(other) for other in also_read if Path(other) != self.path] + [self.path]:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    if record.get("cache_schema") == 2:
                        self._cache[record["key"]] = answered_by(record["answers"], record.get("answered_by"))
                    elif "key" in record and "answer" in record:
                        self.legacy_rows += 1
                        if record["key"] in self._legacy and self._legacy[record["key"]] != record["answer"]:
                            self._legacy_twice.add(record["key"])
                        self._legacy[record["key"]] = record["answer"]
                except (ValueError, KeyError, TypeError):
                    continue
        self.cache_only = cache_only or bool(self.legacy_rows and not allow_new_requests)
        if self.path.exists() and not self.path.read_bytes().endswith(b"\n"):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n")

    @staticmethod
    def identity(inner: Labeller) -> dict:
        return {"name": inner.name, "model": getattr(inner, "model", None),
                "effort": inner.effort, "api_base": masked_all(getattr(inner, "api_base", None)),
                "system_prompt": getattr(inner, "PROMPT", None), "quotes": inner.quotes}

    @classmethod
    def replay(cls, name: str, cache_path: Path, also_read: tuple[Path, ...] = (), identity: dict | None = None,
               quotes: bool | None = None):
        """Select one recorded identity explicitly by name, without credentials/importing a backend.

        Ambiguous configurations require the caller to supply the full recorded identity.
        """
        identities, legacy = [], False
        for path in (*also_read, cache_path):
            if not Path(path).exists():
                continue
            for line in Path(path).read_text().splitlines():
                try:
                    record = json.loads(line)
                    candidate = record.get("identity")
                    if record.get("cache_schema") == 2 and candidate and candidate.get("name") == name and candidate not in identities:
                        identities.append(candidate)
                    legacy = legacy or ("answer" in record and "cache_schema" not in record)
                except (ValueError, TypeError, AttributeError):
                    continue
        if identity is None and not identities and legacy:
            # old rows are keyed by the labeller's name alone; whether it answers quotes changes the questions asked
            # (a chat model through LiteLLM does, and only it: ``LiteLLMLabeller.quotes``)
            identity = {"name": name, "legacy_only": True, "quotes": name.startswith("litellm:") if quotes is None else quotes}
            identities.append(identity)
        if identity is None:
            if len(identities) != 1:
                raise CacheMiss(f"cache replay needs exactly one v2 identity for {name}; found {len(identities)}")
            identity = identities[0]
        elif identity not in identities:
            raise CacheMiss("requested identity is absent from the v2 cache")
        inner = Labeller()
        inner.name, inner.effort, inner.quotes = name, identity.get("effort"), identity.get("quotes", False)
        return cls(inner, cache_path, also_read, cache_only=True, request_identity=identity)

    def _key(self, state: dict, questions: dict, reading: int = 0) -> str:
        blob = json.dumps([2, self.request_identity, state, questions, reading], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _from_legacy(self, state: dict, questions: dict, reading: int) -> dict | None:
        """The old tool's answer to this request, where its rows hold one (see the class)."""
        if not self._legacy:
            return None
        found, rewritten = {}, False
        for name, question in questions.items():
            parts = [self.inner.name, state, question] + ([f"reading {reading}"] if reading else [])
            key = hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            if key not in self._legacy:
                return None
            found[name] = self._legacy[key]
            rewritten = rewritten or key in self._legacy_twice
        self.legacy_rewritten += rewritten
        return found

    def ask(self, state: dict, questions: dict) -> dict:
        return self.again(state, questions, 0)

    def again(self, state: dict, questions: dict, reading: int) -> dict:
        asked = questions  # the old rows were keyed by the questions as they were, the state masked
        state, questions = masked_all(state), masked_all(questions)
        key = self._key(state, questions, reading)
        while True:
            with self._lock:
                if key in self._cache:
                    self.cache_hits += len(questions)
                    return copy.deepcopy(self._cache[key])
                old = self._from_legacy(state, asked, reading)
                if old is not None:
                    old = masked_all(old)
                    self._cache[key] = copy.deepcopy(old)
                    self.cache_hits += len(questions)
                    self.legacy_used += 1
                    # kept also when nothing may be asked: it is the same answer, and costs nothing
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"cache_schema": 2, "key": key, "identity": self.request_identity,
                                                 "answers": old, "from_legacy": True}, ensure_ascii=False) + "\n")
                    return old
                if self.cache_only:
                    raise CacheMiss("a request is in neither the cache nor, whole, in its old rows; new requests are disabled "
                                    "(--allow-new-requests asks the model, which costs money)")
                waiting = self._asking.get(key)
                if waiting is None:
                    self._asking[key] = threading.Event()
                    break
            waiting.wait()
        try:
            raw = self.inner.again(state, questions, reading) if reading else self.inner.ask(state, questions)
            by = getattr(raw, "answered_by", None)  # another model answered it (the labeller's model refused)
            fresh = answered_by(masked_all(raw), by)
            if set(fresh) != set(questions):
                raise LabellerError("incomplete response cannot be cached")
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                row = {"cache_schema": 2, "key": key, "identity": self.request_identity, "answers": fresh}
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row | ({"answered_by": by} if by else {}), ensure_ascii=False) + "\n")
                self._cache[key] = copy.deepcopy(fresh)
        finally:
            with self._lock:
                self._asking.pop(key).set()
            if self.progress:
                self.progress()
        return fresh

    def stats(self) -> dict:
        return {**self.inner.stats(), "cache_hits": self.cache_hits, "cache_schema": 2,
                "legacy_rows": self.legacy_rows, "requests_from_legacy_rows": self.legacy_used,
                "legacy_requests_with_rewritten_rows": self.legacy_rewritten, "cache_only": self.cache_only}


# The groups of questions a labeller of its own can be given (``--labeller-for GROUP=SPEC``); the others go to the
# default one. "fix" is what the checks after a fix attempt show, whether it changed what is wrong, why the agent made
# it, and what its changes reach; "words" is the agent's own words (does it name the true fault, does it change its
# mind, what does it hold responsible, does it take a planted decoy for the cause). On runs no wording had seen,
# deepseek-flash found far more than Jev in those two groups and was as often right when it said yes
# (docs/run-report.md, "Another model for the fix attempts and the agent's own words").
GROUPS = ("submissions", "outputs", "commands", "judge", "answers", "fix", "words")


class Refusals:
    """What one run's requests met: refused by the labeller's model and answered by another (by that model's name),
    or refused by that one too and left unanswered. Counted as the report asks, cached answers included."""

    def __init__(self) -> None:
        self.answered_by: dict[str, int] = {}
        self.unanswered = 0
        self._lock = threading.Lock()

    def add(self, model: str | None) -> None:
        with self._lock:
            if model:
                self.answered_by[model] = self.answered_by.get(model, 0) + 1
            else:
                self.unanswered += 1


class _Counted:
    """A labeller as one run's report uses it, counting into that run's ``Refusals``."""

    def __init__(self, inner: Labeller, refusals: Refusals) -> None:
        self._inner, self._refusals = inner, refusals

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def ask(self, state: dict, questions: dict) -> dict:
        return self._count(lambda: self._inner.ask(state, questions))

    def again(self, state: dict, questions: dict, reading: int) -> dict:
        return self._count(lambda: self._inner.again(state, questions, reading))

    def _count(self, call) -> dict:
        try:
            answers = call()
        except LabellerRefused:
            self._refusals.add(None)
            raise
        by = getattr(answers, "answered_by", None)
        if by:
            self._refusals.add(by)
        return answers


class Labellers:
    """Which labeller answers which group of questions: the default one, and others for named groups."""

    def __init__(self, default: Labeller | None, by_group: dict[str, Labeller | None] | None = None) -> None:
        self.default, self.by_group = default, dict(by_group or {})

    def __call__(self, group: str) -> Labeller | None:
        return self.by_group.get(group, self.default)

    def counted(self) -> tuple["Labellers", Refusals]:
        """The same labellers for one run, and what that run's requests met of the models' refusals."""
        refusals, wrapped = Refusals(), {}

        def wrap(labeller):
            return None if labeller is None else wrapped.setdefault(id(labeller), _Counted(labeller, refusals))

        return Labellers(wrap(self.default), {g: wrap(l) for g, l in self.by_group.items()}), refusals  # noqa: E741

    def distinct(self) -> list[Labeller]:
        found: list[Labeller] = []
        for labeller in [self.default, *self.by_group.values()]:
            if labeller is not None and all(labeller is not other for other in found):
                found.append(labeller)
        return found

    @property
    def workers(self) -> int:
        return max((labeller.workers for labeller in self.distinct()), default=1)

    def names(self) -> dict[str, str | None]:
        """Who answers each group, for the report's source."""
        return {group: (self(group).name if self(group) else None) for group in GROUPS}

    def __bool__(self) -> bool:
        return bool(self.distinct())


def as_labellers(labeller: Labeller | Labellers | None) -> Labellers:
    return labeller if isinstance(labeller, Labellers) else Labellers(labeller)


def make_labellers(
    spec: str,
    for_groups: list[str],
    key_file: Path | None,
    folder: Path,
    workers: int = 1,
    effort: str | None = None,
    chat_workers: int | None = None,
    cache_only: bool = False,
    allow_new_requests: bool = False,
) -> Labellers:
    """The default labeller (``spec``) and one per ``GROUP=SPEC`` of ``for_groups``. Each labeller keeps its answers
    in a cache file of its own (label_cache.jsonl for the default one, label_cache.<spec>.jsonl for the others) and
    reads the other one of the two as well, so a model moved from some groups to all of them, or back, asks again
    nothing it has answered in that folder; one spec serves every group that names it. The
    reasoning effort, and ``chat_workers`` in place of ``workers``, go to the LiteLLM labellers only: Jev has no
    effort to set, and a chat model that reasons at length on every request is the one worth more requests at once."""
    made: dict[str, Labeller | None] = {}

    def own_file(wanted: str) -> Path:
        slug = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in wanted)
        return folder / f"label_cache.{slug}.jsonl"

    def labeller_for(wanted: str, cache: Path) -> Labeller | None:
        if wanted not in made:
            chat = wanted.startswith("litellm:")
            other = own_file(wanted) if cache.name == "label_cache.jsonl" else folder / "label_cache.jsonl"
            made[wanted] = make_labeller(
                wanted,
                key_file,
                cache,
                (chat_workers or workers) if chat else workers,
                effort if chat else None,
                (other,),
                cache_only=cache_only,
                allow_new_requests=allow_new_requests,
            )
        return made[wanted]

    default = labeller_for(spec, folder / "label_cache.jsonl")
    by_group = {}
    for item in for_groups:
        group, _, wanted = item.partition("=")
        if group not in GROUPS or not wanted:
            raise LabellerError(f"--labeller-for wants GROUP=SPEC with GROUP one of {', '.join(GROUPS)}: {item!r}")
        by_group[group] = labeller_for(wanted, own_file(wanted))
    return Labellers(default, by_group)


def make_labeller(
    spec: str,
    key_file: Path | None,
    cache_path: Path | None,
    workers: int = 1,
    effort: str | None = None,
    also_read: tuple[Path, ...] = (),
    cache_only: bool = False,
    allow_new_requests: bool = False,
) -> Labeller | None:
    """``none`` | ``jev`` | ``jev:<model>`` | ``litellm:<model>``."""
    if spec in ("", "none"):
        return None
    if cache_only and cache_path:
        name = (spec.split(":", 1)[1] if ":" in spec else "jev-1.13.0") if spec.startswith("jev") else spec + (f" effort={effort}" if effort else "")
        return CachedLabeller.replay(name, cache_path, also_read)
    if spec.startswith("jev"):
        from clients.jev.config import KEY_ENV

        if effort:
            raise LabellerError("Jev has no reasoning effort to set")
        key = read_key(KEY_ENV, key_file)
        if not key:
            raise LabellerError(f"{KEY_ENV} is not set (environment or --key-file)")
        inner: Labeller = JevLabeller(key, spec.split(":", 1)[1] if ":" in spec else "jev-1.13.0", workers=workers)
    elif spec.startswith("codex:"):
        inner = CodexLabeller(spec.split(":", 1)[1], effort, workers)
    elif spec.startswith("claudecode:"):
        # the environment first, then --key-file, else ~/.config/sregym/claude.env (read_key looks at the environment first)
        token = read_key("CLAUDE_CODE_OAUTH_TOKEN", Path.home() / ".config/sregym/claude.env") if not key_file else None
        token = token or read_key("CLAUDE_CODE_OAUTH_TOKEN", key_file)
        if not token:
            raise LabellerError("CLAUDE_CODE_OAUTH_TOKEN is not set (the subscription token from `claude setup-token`)")
        inner = ClaudeCodeLabeller(spec.split(":", 1)[1], token, effort, workers)
    elif spec.startswith("litellm:"):
        inner = LiteLLMLabeller(
            spec.split(":", 1)[1],
            read_key("LABELLER_API_BASE", key_file),
            read_key("LABELLER_API_KEY", key_file),
            effort,
            workers,
        )
    else:
        raise LabellerError(f"unknown labeller {spec!r}")
    return CachedLabeller(inner, cache_path, also_read, allow_new_requests=allow_new_requests) if cache_path else inner
