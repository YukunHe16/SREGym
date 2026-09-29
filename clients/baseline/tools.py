"""The one tool the baseline agent has: a bash command line, run in the agent container.

It is mini-swe-agent's local environment: each command runs in a fresh subshell with the
cluster's kubectl configured, and a command that runs past its timeout is killed together
with everything it started.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass


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
