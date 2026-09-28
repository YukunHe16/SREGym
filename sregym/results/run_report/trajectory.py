"""Read one SREGym run from its ATIF ``trajectory.json``.

SREGym converts every agent's native log to ATIF, so one reader serves all agents; what a tool call looks like is
still the agent's own (whole suites have been read for baseline, codex and GitHub Copilot's CLI; of Claude Code only
upstream's sample run). Standard library only, except for runs recorded before SREGym wrote the file, which go
through SREGym's own converter.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .masking import masked

SHELL_KEYS = ("command", "cmd", "script")
# What a submission looks like by its text alone. It is the judgement every run starts with; with a labeller the
# report asks about each command that may be one (``labels.judge_submissions``), and the answer replaces it.
SUBMIT = re.compile(r"/submit\b|submit_tool|\bsubmit\b\s*\(", re.IGNORECASE)
ONLY_SUBMITS, SUBMITS_AND_MORE = "only_submits", "submits_and_more"

# Codex has one tool, ``exec``, whose argument is a JavaScript snippet that calls the real tools:
#   const r = await tools.exec_command({cmd: "kubectl get pods", workdir: "/logs", ...}); text(r.output);
# and its result starts with a header of its own: "Script completed / Wall time 0.3 seconds / Output:".
CODE_SHELL_KEY = re.compile(r"""(?:\bcmd|"cmd"|'cmd')\s*:\s*(?=["'`])""")
CODE_WORKDIR = re.compile(r""",?\s*["']?workdir["']?\s*:\s*"[^"]*\"""")
# (Copilot's CLI runs the same kind of script in a cell and reports "Script running with cell ID 1" when the script
# outlives its wait; a later ``wait`` call brings the rest.)
CODE_RESULT_HEADER = re.compile(r"\AScript (\w+)(?: with cell ID \S+)?\nWall time ([\d.]+) seconds\n(?:Output:\n+)?")
# What ``tools.exec_command`` returns to the script: the command's output and Codex's own bookkeeping around it. A
# script that prints the whole object (``text(JSON.stringify(r))``) puts that bookkeeping on screen too.
EXEC_RESULT_KEYS = {"chunk_id", "wall_time_seconds", "session_id", "original_token_count", "exit_code", "output"}
# GitHub Copilot's CLI appends its own record of a result to the text the model was shown: a JSON object holding the
# same text again, or a diff of the file it touched ("detailedContent"), and for a shell its exit status and working
# directory ("contents"). It is left out, so the output is what the agent saw and is not sent to a labeller twice.
AGENT_DETAIL = '\n{"detailedContent": '
# Claude Code's converter does the same after a blank line: the output again ("[stdout]", "[stderr]"), the exit code,
# and "[metadata] {...}" with whatever else the tool reported.
TOOL_RECORD = re.compile(r"(?:\A|\n\n)(?:\[(?:stdout|stderr)\]\n|\[(?:exit_code|interrupted|is_image|metadata)\] )")
# Calls with which an agent keeps its own notes and touches neither the cluster nor the benchmark: Copilot's to-do
# table in its session database, and listing or stopping its own background shells. They are not actions of the run.
BOOKKEEPING_TOOLS = {"list_bash", "stop_bash"}
SESSION_TABLES = re.compile(r"\btodos?\b|\btodo_deps\b", re.IGNORECASE)
ATIF_MARK = re.compile(rb'"schema_version"\s*:\s*"ATIF')
RUN_FOLDER = re.compile(
    r"run_\d+"
)  # one attempt's folder in SREGym's layout, results/<batch>/<agent>/<problem>/run_<n>
JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "\n": ""}
JS_CODE_POINT = re.compile(r"x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|u\{([0-9a-fA-F]+)\}")


@dataclass
class Action:
    """One tool call and what came back."""

    step: int
    stage: str  # diagnosis | mitigation | unknown
    tool: str
    command: str  # the shell command, or "<tool> <arguments>" for a non-shell tool
    output: str
    duration_s: float | None = None
    extra: dict = field(default_factory=dict)
    # None, ONLY_SUBMITS (it sends an answer and nothing else) or SUBMITS_AND_MORE (kubectl ...; curl .../submit)
    submits: str | None = None
    # the shell commands a Codex script started (``tools.exec_command`` calls); None for a plain command, or a script
    # whose commands are built at run time
    calls: int | None = None


@dataclass
class Turn:
    """One model reply."""

    step: int
    stage: str
    n_actions: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    latency_s: float | None
    timestamp: float | None = None  # seconds since the epoch; every agent's file read so far says when a reply came
    message: str = ""  # what the agent wrote in this reply, as it wrote it (a baseline agent's holds its command)
    reasoning: str = ""  # its reasoning, when the file keeps it
    model: str | None = None  # the model that wrote it, where the file says so step by step


# Messages that reach the agent in the middle of a run, from outside it: an experiment's advice or nudge, the task sent
# again to a model that takes over, a turn Codex interrupted (2026-09-26: the model-handover pilot of sre-router, whose
# arms put a senior engineer's advice, a false "your submission was not recorded", or a stronger model into the run).
# Codex's own notes that come with each new turn (the working directory, its skills) are not messages to the agent.
CODEX_CONTEXT = re.compile(r"\A\s*<(?:environment_context|skills_instructions|user_instructions|permissions)\b")
TURN_ABORTED = re.compile(r"\A\s*<turn_aborted>")


@dataclass
class Run:
    path: Path
    agent: str
    agent_version: str | None
    model: str | None
    reasoning_effort: str | None
    agent_extra: dict
    sregym: dict
    turns: list[Turn]
    actions: list[Action]
    stages_recorded: bool = True  # False: the file said nothing, the stages are split at the first submission
    # messages from outside the agent after its first reply: {"step", "kind": message | task_again | interrupted,
    # "source", "text"}; see CODEX_CONTEXT
    messages: list[dict] = field(default_factory=list)

    @property
    def problem_id(self) -> str | None:
        return self.sregym.get("problem_id")

    def split_at_first_submission(self) -> None:
        """For a file that records no stage: the diagnosis stage ends with the first submission."""
        if self.stages_recorded:
            return
        first = next((a.step for a in self.actions if a.submits), None)
        for item in (*self.turns, *self.actions):
            item.stage = "unknown" if first is None else "diagnosis" if item.step <= first else "mitigation"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(v.get("text") if isinstance(v, dict) and "text" in v else v) for v in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _content(value: Any) -> str:
    """A tool result as text. Codex results reach ATIF as the ``str()`` of a list of text parts; read them back."""
    if isinstance(value, str) and value.startswith("[{'type': ") and value.endswith("}]"):
        try:
            parts = ast.literal_eval(value)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return value
        # anything else that merely looks like a list (a JSON array an agent printed) stays as it was printed
        if all(isinstance(p, dict) and set(p) <= {"type", "text"} and isinstance(p.get("text"), str) for p in parts):
            return "\n".join(p["text"] for p in parts)
        return value
    return _text(value)


def _js_string(source: str, start: int) -> tuple[str, int] | None:
    """The JavaScript string literal that opens at ``start`` and where it ends; None if it is built at run time."""
    quote, out, i = source[start], [], start + 1
    while i < len(source):
        ch = source[i]
        if ch == quote:
            if source[i + 1 :].lstrip()[:1] not in (",", "}"):
                return None  # "a" + b
            text = "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
            return text, i + 1
        if quote == "`" and source.startswith("${", i):
            return None
        if ch == "\\" and i + 1 < len(source):
            code_point = JS_CODE_POINT.match(source, i + 1)
            if code_point:
                out.append(chr(int(next(g for g in code_point.groups() if g), 16)))
                i = code_point.end()
            else:
                out.append(JS_ESCAPES.get(source[i + 1], source[i + 1]))
                i += 2
            continue
        out.append(ch)
        i += 1
    return None


def _exec_result_output(text: str) -> str:
    """The command's output out of a Codex ``exec_command`` result object printed whole; the text as it was otherwise.

    The wrapper (``{"chunk_id": ..., "session_id": ..., "output": "..."}``) is Codex's, not something the command
    put on screen: read as it stands it passes for a record the benchmark keeps (a labeller called one such
    ``port-forward`` output SREGym's own material).
    """
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}") and '"chunk_id"' in stripped[:200]):
        return text
    try:
        result = json.loads(stripped)
    except ValueError:
        return text
    if isinstance(result, dict) and isinstance(result.get("output"), str) and set(result) <= EXEC_RESULT_KEYS:
        return result["output"]
    return text


def _without_agent_detail(text: str) -> str:
    """The text without the record Copilot's CLI appends to it (``AGENT_DETAIL``); the text itself otherwise."""
    cut = text.rfind(AGENT_DETAIL)
    if cut < 0:
        return text
    try:
        detail = json.loads(text[cut + 1 :])
    except ValueError:
        return text
    if isinstance(detail, dict) and "detailedContent" in detail and set(detail) <= {"detailedContent", "contents"}:
        return text[:cut]
    return text


def _without_tool_record(text: str) -> str:
    """The text without the record Claude Code's converter appends to it (``TOOL_RECORD``). Only Claude Code's
    results are cut this way: in another agent's output the same words would be what the command printed."""
    match = TOOL_RECORD.search(text)
    return text[: match.start()] if match else text


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _bookkeeping(tool: str, arguments: Any) -> bool:
    if tool in BOOKKEEPING_TOOLS:
        return True
    query = arguments.get("query") if isinstance(arguments, dict) else None
    return tool == "sql" and isinstance(query, str) and bool(SESSION_TABLES.search(query))


def find_trajectories(path: Path | str) -> list[Path]:
    """The runs at ``path``: the file itself, or every ``trajectory.json`` under the directory; where there is none,
    every JSON file under it that opens as an ATIF trajectory (some harnesses name them after the attempt,
    ``<problem>/run1.json``)."""
    path = Path(path)
    if path.is_file():
        return [path]
    found = sorted(path.rglob("trajectory.json"))
    if found:
        return found
    atif = []
    for candidate in sorted(path.rglob("*.json")):
        with candidate.open("rb") as handle:
            if ATIF_MARK.search(handle.read(4096)):
                atif.append(candidate)
    return atif


def run_folders_without_trajectory(path: Path | str) -> list[Path]:
    """The ``run_<n>`` folders at ``path`` that hold no ``trajectory.json``: runs from before SREGym wrote one (upstream
    #918, 5 July 2026), or ones whose conversion failed at the time."""
    path = Path(path)
    if path.is_file():
        return []
    folders = [path] if RUN_FOLDER.fullmatch(path.name) else sorted(path.rglob("run_*"))
    return [f for f in folders if f.is_dir() and RUN_FOLDER.fullmatch(f.name) and not (f / "trajectory.json").exists()]


def convert_run_folder(run_dir: Path) -> tuple[dict | None, str]:
    """ATIF for a run folder that has none, made in memory by SREGym's own converter from the agent's session files,
    which the harness keeps under ``sessions/``; nothing is written into the folder. (None, why) when it cannot be."""
    try:
        from sregym.traces import convert
    except ImportError as error:  # the converter needs pydantic, which only SREGym's environment has
        return None, f"SREGym's converter is not available here ({error})"
    # the converter reports each unconvertible run as a warning of its own; the report says why it was skipped
    quiet = logging.getLogger("sregym.traces")
    level = quiet.level
    quiet.setLevel(logging.ERROR)
    try:
        trajectory = convert.convert_run(run_dir)
    except Exception as error:  # an unknown agent or a damaged session file
        return None, f"SREGym's converter could not read it ({error})"
    finally:
        quiet.setLevel(level)
    if trajectory is None:
        return None, "no session files the converter can read (a run that kept only driver.log)"
    return trajectory.to_json_dict(), ""


def _shell_in_code(code: str) -> str:
    """The shell commands inside a Codex script, one per line; the script itself if one is built at run time."""
    commands, position = [], 0
    while match := CODE_SHELL_KEY.search(code, position):
        literal = _js_string(code, match.end())
        if literal is None:
            commands = []
            break
        commands.append(literal[0])
        position = literal[1]
    # The harness starts Codex in /logs, so a bare ``workdir: "/logs"`` is not a path the agent went looking for.
    return "\n".join(commands) if commands else CODE_WORKDIR.sub("", code).strip()


def _shell_calls(arguments: Any) -> int | None:
    """How many shell commands a Codex script starts, where its text says (``_shell_in_code``); None otherwise."""
    code = arguments.get("input") if isinstance(arguments, dict) else None
    if not isinstance(code, str) or "tools." not in code or any(key in arguments and arguments[key] for key in SHELL_KEYS):
        return None
    count, position = 0, 0
    while match := CODE_SHELL_KEY.search(code, position):
        literal = _js_string(code, match.end())
        if literal is None:
            return None
        count, position = count + 1, literal[1]
    return count or None


def _command_of(tool: str, arguments: Any) -> str:
    if isinstance(arguments, dict):
        for key in SHELL_KEYS:
            if key in arguments and arguments[key]:
                value = arguments[key]
                return " ".join(map(str, value)) if isinstance(value, list) else str(value)
        code = arguments.get("input")
        if isinstance(code, str) and "tools." in code:
            return _shell_in_code(code)
        return f"{tool} {json.dumps(arguments, ensure_ascii=False, default=str)}"
    return f"{tool} {_text(arguments)}".strip()


def load_run(path: Path | str, data: dict | None = None) -> Run:
    """The run in the file at ``path``, or in ``data`` when it was converted in memory (``path`` then names the file
    it would have been)."""
    path = Path(path)
    if data is None:
        data = json.loads(path.read_text(encoding="utf-8"))
    agent = data.get("agent") or {}
    claude_code = agent.get("name") == "claudecode"
    extra = data.get("extra") or {}
    sregym = extra.get("sregym") or {}
    boundary = sregym.get("diagnosis_submitted_step")

    turns: list[Turn] = []
    actions: list[Action] = []
    messages: list[dict] = []
    task_texts: set[str] = set()  # what the user said before the agent's first reply: the task
    effort = None
    for step in data.get("steps") or []:
        if step.get("source") not in (None, "agent"):
            text = _text(step.get("message")).strip()
            if not turns:
                task_texts.add(text)
            elif text and not CODEX_CONTEXT.match(text):
                kind = "interrupted" if TURN_ABORTED.match(text) else "task_again" if text in task_texts else "message"
                # the task again with more after it: a model taking over is given the task and a note on the work so
                # far ("## Handoff from a previous session", with the earlier model's hypothesis and its commands)
                task = next((x for x in task_texts if len(x) > 200 and text.startswith(x)), None)
                if kind == "message" and task:
                    kind, text = "task_again", text[len(task):].strip()
                elif kind == "task_again":
                    text = ""
                if kind == "message" and step.get("source") == "system":
                    continue  # the harness's own instructions to a new turn, not one of the run's messages
                messages.append({"step": int(step.get("step_id") or 0), "kind": kind, "source": step.get("source"),
                                 "text": "" if kind == "interrupted" else masked(text)})
            continue
        step_id = int(step.get("step_id") or len(turns) + 1)
        step_extra = step.get("extra") or {}
        # The diagnosis stage ends with the step that submitted the diagnosis: what came on screen later cannot have
        # informed it. The harness's own marker stays "diagnosis" for a few more steps, while the judge works, so it
        # is used only when no submission step was recorded.
        if isinstance(boundary, int):
            stage = "diagnosis" if step_id <= boundary else "mitigation"
        else:
            stage = step_extra.get("stage") if step_extra.get("stage") in ("diagnosis", "mitigation") else "unknown"
        effort = effort or step.get("reasoning_effort")
        metrics = step.get("metrics") or {}
        calls = [
            c
            for c in step.get("tool_calls") or []
            if not _bookkeeping(str(c.get("function_name") or ""), c.get("arguments"))
        ]
        turns.append(
            Turn(
                step=step_id,
                stage=stage,
                n_actions=len(calls),
                prompt_tokens=metrics.get("prompt_tokens"),
                completion_tokens=metrics.get("completion_tokens"),
                cached_tokens=metrics.get("cached_tokens"),
                reasoning_tokens=(metrics.get("extra") or {}).get("reasoning_output_tokens"),
                latency_s=step_extra.get("latency_s"),
                timestamp=_timestamp(step.get("timestamp")),
                message=masked(_text(step.get("message"))),
                reasoning=masked(_text(step.get("reasoning_content"))),
                model=step.get("model_name"),
            )
        )
        results = {r.get("source_call_id"): r for r in ((step.get("observation") or {}).get("results") or [])}
        for call in calls:
            tool = str(call.get("function_name") or "")
            command = masked(_command_of(tool, call.get("arguments")))
            result = results.get(call.get("tool_call_id")) or {}
            result_extra = result.get("extra") or {}
            output = masked(_without_agent_detail(_content(result.get("content"))))
            duration = result_extra.get("duration_s")
            if claude_code:
                output = _without_tool_record(output)
            header = CODE_RESULT_HEADER.match(output)
            if header:
                status, wall_time = header.groups()
                body = _exec_result_output(output[header.end() :])
                output = ("" if status == "completed" else f"Script {status}\n") + body
                duration = float(wall_time) if duration is None else duration
            actions.append(
                Action(
                    step=step_id,
                    stage=stage,
                    tool=tool,
                    command=command,
                    output=output,
                    duration_s=duration,
                    extra=result_extra,
                    submits=ONLY_SUBMITS if SUBMIT.search(command) or "submit" in tool.lower() else None,
                    calls=_shell_calls(call.get("arguments")),
                )
            )
    run = Run(
        path=path,
        agent=str(agent.get("name") or "unknown"),
        agent_version=agent.get("version"),
        model=agent.get("model_name"),
        reasoning_effort=effort or (agent.get("extra") or {}).get("reasoning_effort"),
        agent_extra=agent.get("extra") or {},
        sregym=sregym,
        turns=turns,
        actions=actions,
        stages_recorded=any(a.stage != "unknown" for a in actions),
        messages=messages,
    )
    run.split_at_first_submission()
    return run
