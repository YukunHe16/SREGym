"""Tools the baseline agent can call: a few local ones and whatever SREGym's MCP server offers.

Local tools run inside the agent container (``bash`` with the cluster's kubectl configured,
``read_file``, ``write_file``). MCP tools are discovered at start-up from the sub-servers of
``MCP_SERVER_URL`` (kubectl, prometheus, jaeger, loki), the same server the Stratus agent uses,
and are exposed to the model under their own names. Submission is not a tool from here: the
driver owns it so that budgets and submission modes stay enforceable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TOOLS = "bash,read_file,write_file,mcp:kubectl,mcp:prometheus,mcp:jaeger,mcp:loki"
MCP_SERVERS = ("kubectl", "prometheus", "jaeger", "loki")
MCP_CONNECT_TIMEOUT_S = 30
MCP_CALL_TIMEOUT_S = 120
MAX_FILE_CHARS = 200_000


@dataclass
class ToolSpec:
    """One function the model may call, in OpenAI function-calling shape."""

    name: str
    description: str
    parameters: dict
    kind: str  # "local" | "mcp" | "submit"
    server: str | None = None  # MCP sub-server for kind == "mcp"

    def as_openai(self) -> dict:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False


def run_command(command: str, timeout: int, cwd: str | None = None) -> CommandResult:
    """Run one command line through bash; kill the whole process group on timeout."""
    started = time.monotonic()
    with subprocess.Popen(
        ["bash", "-lc", command],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return CommandResult(stdout, stderr, proc.returncode, round(time.monotonic() - started, 3))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            return CommandResult(
                stdout,
                stderr + f"\n[command timed out after {timeout}s and was killed]",
                124,
                round(time.monotonic() - started, 3),
                timed_out=True,
            )


LOCAL_TOOLS: dict[str, ToolSpec] = {
    "bash": ToolSpec(
        "bash",
        "Run one non-interactive bash command line in the agent container. kubectl is configured for the "
        "cluster. Returns stdout, stderr (each truncated) and the exit code.",
        {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command line to run."}},
            "required": ["command"],
        },
        "local",
    ),
    "read_file": ToolSpec(
        "read_file",
        "Read a text file from the agent container's filesystem.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "local",
    ),
    "write_file": ToolSpec(
        "write_file",
        "Write text to a file in the agent container (parent directories are created). Useful for manifests.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        "local",
    ),
}


def parse_tool_selection(value: str | None) -> tuple[list[str], list[str]]:
    """``BASELINE_TOOLS`` -> (local tool names, MCP sub-servers)."""
    local: list[str] = []
    servers: list[str] = []
    for item in (value or DEFAULT_TOOLS).split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("mcp:"):
            server = item[4:]
            if server not in MCP_SERVERS:
                raise ValueError(f"unknown MCP sub-server {server!r}; known: {MCP_SERVERS}")
            servers.append(server)
        elif item in LOCAL_TOOLS:
            local.append(item)
        else:
            raise ValueError(f"unknown tool {item!r}; known: {sorted(LOCAL_TOOLS)} and mcp:<server>")
    return local, servers


def run_local_tool(name: str, args: dict, *, timeout: int, cwd: str | None) -> tuple[str, dict]:
    """Execute a local tool; returns (text for the model, record for the transcript)."""
    if name == "bash":
        result = run_command(str(args.get("command", "")), timeout, cwd=cwd)
        text = (
            f"exit_code: {result.exit_code}"
            + (" (timed out)" if result.timed_out else "")
            + "\nstdout:\n"
            + (result.stdout or "(empty)")
            + "\nstderr:\n"
            + (result.stderr or "(empty)")
        )
        record = {
            "exit_code": result.exit_code,
            "duration_s": result.duration_s,
            "timed_out": result.timed_out,
            "stdout_chars": len(result.stdout),
            "stderr_chars": len(result.stderr),
        }
        return text, record
    if name == "read_file":
        path = Path(str(args.get("path", "")))
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"error: {exc}", {"error": str(exc)}
        if len(data) > MAX_FILE_CHARS:
            data = data[:MAX_FILE_CHARS] + f"\n[... file truncated at {MAX_FILE_CHARS} chars ...]"
        return data, {"chars": len(data)}
    if name == "write_file":
        path = Path(str(args.get("path", "")))
        content = str(args.get("content", ""))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"error: {exc}", {"error": str(exc)}
        return f"wrote {len(content)} chars to {path}", {"chars": len(content)}
    raise ValueError(f"not a local tool: {name}")


def _content_text(result) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None:
            dump = getattr(block, "model_dump", None)
            text = json.dumps(dump(), default=str) if callable(dump) else str(block)
        parts.append(text)
    text = "\n".join(parts)
    if getattr(result, "isError", False):
        text = "error: " + text
    return text


class McpToolbox:
    """Keeps one SSE session per MCP sub-server alive on a background event loop.

    The kubectl server keys its per-agent state (rollback history) on the ``sregym_ssid``
    header, so every session sends the run's artifact id.
    """

    def __init__(self, base_url: str, servers: list[str], session_id: str):
        self.base_url = base_url.rstrip("/")
        self.servers = list(servers)
        self.session_id = session_id
        self.specs: list[ToolSpec] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._sessions: dict = {}
        self._stops: dict = {}
        self._tasks: list = []

    # -- lifecycle -----------------------------------------------------------------
    def connect(self) -> list[ToolSpec]:
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
        self.specs = future.result(MCP_CONNECT_TIMEOUT_S * (len(self.servers) + 1))
        return self.specs

    async def _connect_all(self) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for server in self.servers:
            ready: asyncio.Future = self._loop.create_future()
            stop = asyncio.Event()
            self._stops[server] = stop
            self._tasks.append(asyncio.ensure_future(self._serve(server, ready, stop)))
            tools = await asyncio.wait_for(ready, MCP_CONNECT_TIMEOUT_S)
            specs.extend(tools)
        names = [s.name for s in specs]
        for spec in specs:  # disambiguate only when two servers export the same tool name
            if names.count(spec.name) > 1:
                spec.name = f"{spec.server}__{spec.name}"
        return specs

    async def _serve(self, server: str, ready: asyncio.Future, stop: asyncio.Event) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        url = f"{self.base_url}/{server}/sse"
        try:
            async with (
                sse_client(url=url, headers={"sregym_ssid": self.session_id}) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                listed = await session.list_tools()
                self._sessions[server] = session
                ready.set_result(
                    [
                        ToolSpec(
                            t.name,
                            (t.description or "").strip(),
                            t.inputSchema or {"type": "object", "properties": {}},
                            "mcp",
                            server,
                        )
                        for t in listed.tools
                    ]
                )
                await stop.wait()
        except BaseException as exc:  # connection failures surface through ``ready``
            if not ready.done():
                ready.set_exception(RuntimeError(f"MCP {server} at {url}: {type(exc).__name__}: {exc}"))
            else:
                raise

    def close(self) -> None:
        async def _stop_all():
            for stop in self._stops.values():
                stop.set()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)

        if self._thread.is_alive():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_stop_all(), self._loop).result(10)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)

    # -- calls ---------------------------------------------------------------------
    def call(self, spec: ToolSpec, args: dict) -> str:
        async def _call():
            session = self._sessions[spec.server]
            raw_name = spec.name.split("__", 1)[1] if "__" in spec.name else spec.name
            result = await asyncio.wait_for(session.call_tool(raw_name, args), MCP_CALL_TIMEOUT_S)
            return _content_text(result)

        return asyncio.run_coroutine_threadsafe(_call(), self._loop).result(MCP_CALL_TIMEOUT_S + 10)


@dataclass
class Toolbox:
    """Everything callable in one run: local tools, MCP tools, and the driver-owned submit."""

    local: list[str] = field(default_factory=list)
    mcp: McpToolbox | None = None
    command_timeout: int = 60
    work_dir: str | None = None

    def specs(self) -> list[ToolSpec]:
        out = [LOCAL_TOOLS[name] for name in self.local]
        if self.mcp is not None:
            out.extend(self.mcp.specs)
        return out

    def find(self, name: str) -> ToolSpec | None:
        return next((s for s in self.specs() if s.name == name), None)

    def call(self, spec: ToolSpec, args: dict) -> tuple[str, dict]:
        if spec.kind == "local":
            return run_local_tool(spec.name, args, timeout=self.command_timeout, cwd=self.work_dir)
        assert self.mcp is not None
        started = time.monotonic()
        try:
            text = self.mcp.call(spec, args)
            return text, {"duration_s": round(time.monotonic() - started, 3), "chars": len(text)}
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}", {
                "duration_s": round(time.monotonic() - started, 3),
                "error": str(exc)[:300],
            }
