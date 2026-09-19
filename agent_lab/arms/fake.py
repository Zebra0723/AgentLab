"""A test double in arm's clothing.

This arm exists to prove two things without spending a real agent:

  * every referee kill condition fires through the real supervision path, and
  * the arm seam works - this module was added without editing runner.py,
    referee.py or scorer.py, and `python -m agent_lab.runner --list` picks it
    up because it is here.

It is not one of the experiment's arms. Point it at a script with
--fake-script; see tests/scripts/.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..agentproc import run_agent
from ..referee import Referee
from ..sandbox import Workspace
from .base import Arm, RunResult, Task, register

FAKE_AGENT = Path(__file__).resolve().parent.parent / "fakeagent.py"


@register
class FakeArm(Arm):
    name = "fake"
    description = "test double: replays a scripted agent (not an experiment arm)"

    def run(self, task: Task, workspace: Workspace, referee: Referee) -> RunResult:
        script = task.extra.get("fake_script")
        if not script:
            raise ValueError("the fake arm needs a script: pass --fake-script <path>")
        process = run_agent(
            prompt=task.prompt,
            workspace=workspace,
            referee=referee,
            config=self.config,
            label=self.name,
            argv=[sys.executable, str(FAKE_AGENT), "--script", str(Path(script).resolve())],
        )
        verdict = referee.verdict
        return RunResult(
            deploy_url=referee.ledger.deploy_urls[-1] if referee.ledger.deploy_urls else None,
            agent_reported_done=bool(verdict and verdict.condition.value == "agent_done"),
            notes=f"fake script={Path(script).name} exit={process.exit_code} killed={process.killed}",
            processes=[process],
        )
