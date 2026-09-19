"""Launching an agent process and supervising it on the referee's behalf.

This is the only place in agent-lab that starts a child process, and the only
place that can kill one. Arms call `run_agent`; they do not get a handle on the
process, so an arm cannot outlive or shield its own agent.

The supervision loop does four things per line of agent output:
  1. redact credentials,
  2. append the line to the transcript and flush (a run that dies mid-flight
     still leaves a complete transcript),
  3. parse it into Actions and hand them to the referee,
  4. if the referee returns a verdict, kill the process group.

It also commits the workspace after every successful deploy, so the git trail
is the harness's doing rather than something the agent has to remember.
"""

from __future__ import annotations

import functools
import json
import os
import queue
import re
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .events import ActionKind, TranscriptParser
from .redact import Redactor
from .referee import Referee
from .sandbox import Workspace

STDOUT, STDERR = "out", "err"
_STDERR_TAIL_LINES = 40


@dataclass
class AgentProcessResult:
    """What the harness knows about one agent process after it ended."""

    label: str
    argv: list[str]
    exit_code: int | None = None
    killed: bool = False
    transcript: Path | None = None
    stderr_tail: str = ""
    session_id: str | None = None
    transcript_lines: int = 0
    result_subtype: str | None = None
    total_cost_usd: float | None = None
    # Tool calls the harness refused. A non-zero count here means the agent was
    # fighting the setup, not the task.
    permission_denials: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "killed": self.killed,
            "transcript": self.transcript.name if self.transcript else None,
            "transcript_lines": self.transcript_lines,
            "session_id": self.session_id,
            "result_subtype": self.result_subtype,
            "total_cost_usd": self.total_cost_usd,
            "permission_denials": self.permission_denials,
            "stderr_tail": self.stderr_tail,
        }


def build_env(config: Config, env: dict[str, str] | None = None) -> dict[str, str]:
    """The child's environment: an allowlist, plus credentials, nothing else.

    Everything not named in [agent].env_passthrough is dropped - in particular
    CLAUDE_CODE_*, so an agent process never inherits the harness's own session.
    """
    source = dict(os.environ if env is None else env)
    child = {name: source[name] for name in config.agent.env_passthrough if name in source}
    for name in config.agent.grant_credentials:
        if source.get(name):
            child[name] = source[name]
    # Belt and braces: the agent is not the harness.
    child.pop("CLAUDE_CODE_SESSION_ID", None)
    child["AGENT_LAB_RUN"] = "1"
    return child


# --permission-prompts was added in Claude Code 2.1.259. An older binary
# rejects it as an unknown option and exits before writing a single line, which
# the referee sees as an arm that never ran. Omitting it costs nothing in a -p
# run with no permission host: those requests are denied either way.
PERMISSION_PROMPTS_SINCE = (2, 1, 259)


@functools.lru_cache(maxsize=8)
def agent_version(binary: str) -> tuple[int, ...]:
    """The agent binary's version, or () if it cannot be determined."""
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ()
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", done.stdout or "")
    return tuple(int(part) for part in found.groups()) if found else ()


def build_argv(config: Config, prompt: str, session_id: str | None = None) -> list[str]:
    """The headless Claude Code invocation.

    `--output-format stream-json` with `--verbose` is what makes the transcript
    (and therefore the referee) possible: one JSON object per line, streamed as
    the run happens rather than at the end.
    """
    argv = [
        config.agent.binary,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", config.agent.permission_mode,
        "--session-id", session_id or str(uuid.uuid4()),
    ]
    # Nobody is at the keyboard: anything that would prompt is denied rather
    # than left hanging - on binaries new enough to accept the flag.
    if agent_version(config.agent.binary) >= PERMISSION_PROMPTS_SINCE:
        argv += ["--permission-prompts", "none"]
    if config.agent.allowed_tools:
        argv += ["--allowedTools", ",".join(config.agent.allowed_tools)]
    if config.agent.bare:
        argv.append("--bare")
    if config.agent.model:
        argv += ["--model", config.agent.model]
    if config.agent.max_turns > 0:
        argv += ["--max-turns", str(config.agent.max_turns)]
    if config.agent.include_partial_messages:
        argv.append("--include-partial-messages")
    argv += list(config.agent.extra_args)
    return argv


def run_agent(
    prompt: str,
    workspace: Workspace,
    referee: Referee,
    config: Config,
    label: str = "agent",
    redactor: Redactor | None = None,
    argv: list[str] | None = None,
) -> AgentProcessResult:
    """Run one agent process under the referee. Returns when the process is gone."""
    redactor = redactor or Redactor.from_env()
    command = argv if argv is not None else build_argv(config, prompt)
    transcript = workspace.transcript_path(label)
    return _supervise(
        argv=command,
        cwd=workspace,
        env=build_env(config),
        referee=referee,
        config=config,
        transcript=transcript,
        label=label,
        redactor=redactor,
        workspace=workspace,
    )


def _pump(stream: Iterable[str], sink: "queue.Queue[tuple[str, str | None]]", tag: str) -> None:
    try:
        for line in stream:
            sink.put((tag, line))
    except (ValueError, OSError):
        pass
    finally:
        sink.put((tag, None))


def _supervise(
    argv: list[str],
    cwd: Workspace,
    env: dict[str, str],
    referee: Referee,
    config: Config,
    transcript: Path,
    label: str,
    redactor: Redactor,
    workspace: Workspace | None,
) -> AgentProcessResult:
    result = AgentProcessResult(label=label, argv=list(argv), transcript=transcript)
    parser = TranscriptParser(config.referee.detect, agent=label)
    tick = config.referee.tick_seconds
    grace = config.referee.kill_grace_seconds

    transcript.parent.mkdir(parents=True, exist_ok=True)
    referee.start()

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        # Its own process group, so one signal reaches the agent and every
        # shell it started.
        start_new_session=True,
    )

    lines: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, lines, STDOUT), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, lines, STDERR), daemon=True),
    ]
    for t in threads:
        t.start()

    open_streams = {STDOUT, STDERR}
    stderr_tail: list[str] = []
    kill_deadline: float | None = None

    with transcript.open("a", encoding="utf-8") as sink:

        def stop() -> None:
            nonlocal kill_deadline
            if result.killed:
                return
            result.killed = True
            _signal_group(proc, signal.SIGTERM)
            kill_deadline = referee.elapsed + grace

        def overdue() -> bool:
            """True once a killed agent has had its grace period and is still here."""
            return kill_deadline is not None and referee.elapsed > kill_deadline

        while open_streams:
            # An agent that ignores SIGTERM and keeps talking must still die, so
            # this is checked on every pass, not only when the stream goes quiet.
            if overdue():
                _signal_group(proc, signal.SIGKILL)
                break
            try:
                tag, line = lines.get(timeout=tick)
            except queue.Empty:
                # Quiet agent: the clock still runs.
                if referee.tick() is not None:
                    stop()
                if overdue():
                    _signal_group(proc, signal.SIGKILL)
                    break
                if proc.poll() is not None and lines.empty():
                    # Process gone and nothing buffered; give the pumps a beat
                    # to post their EOF markers, then stop waiting.
                    if not any(t.is_alive() for t in threads):
                        break
                continue

            if line is None:
                open_streams.discard(tag)
                continue

            if tag == STDERR:
                stderr_tail.append(redactor.text(line.rstrip("\n")))
                del stderr_tail[:-_STDERR_TAIL_LINES]
                continue

            safe, obj = redactor.json_line(line.rstrip("\n"))
            sink.write(safe + "\n")
            sink.flush()
            result.transcript_lines += 1
            if obj is None:
                continue

            result.session_id = obj.get("session_id") or result.session_id
            if obj.get("type") == "result":
                result.result_subtype = obj.get("subtype")
                cost = obj.get("total_cost_usd")
                result.total_cost_usd = float(cost) if isinstance(cost, (int, float)) else None
                denials = obj.get("permission_denials")
                result.permission_denials = len(denials) if isinstance(denials, list) else 0

            for action in parser.feed(obj, referee.elapsed):
                verdict = referee.observe(action)
                # The harness keeps the git trail, not the agent.
                if action.kind is ActionKind.DEPLOY_OK and workspace is not None:
                    workspace.git_commit(f"deploy {referee.ledger.deploy_successes}: {action.url}")
                if verdict is not None and not result.killed:
                    stop()

    try:
        result.exit_code = proc.wait(timeout=max(grace, 5))
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        try:
            result.exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            result.exit_code = None

    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass

    result.stderr_tail = "\n".join(stderr_tail)
    return result


def _signal_group(proc: subprocess.Popen[str], sig: int) -> None:
    """Signal the whole process group; fall back to the process itself."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass
