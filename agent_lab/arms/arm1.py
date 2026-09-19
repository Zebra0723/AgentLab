"""Arm 1: a single agent, one shot.

One agent process, one prompt, no retries and no second opinion. The referee
decides when it ends.
"""

from __future__ import annotations

from ..agentproc import run_agent
from ..referee import Referee
from ..sandbox import Workspace
from .base import Arm, RunResult, Task, register


@register
class Arm1(Arm):
    name = "arm1"
    description = "single agent, one shot"

    def run(self, task: Task, workspace: Workspace, referee: Referee) -> RunResult:
        # One agent, so the default label: its transcript is transcript.jsonl.
        process = run_agent(
            prompt=task.prompt,
            workspace=workspace,
            referee=referee,
            config=self.config,
        )
        verdict = referee.verdict
        return RunResult(
            deploy_url=referee.ledger.deploy_urls[-1] if referee.ledger.deploy_urls else None,
            agent_reported_done=bool(verdict and verdict.condition.value == "agent_done"),
            notes=f"exit={process.exit_code} killed={process.killed} result={process.result_subtype}",
            processes=[process],
        )
