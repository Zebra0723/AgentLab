"""The arm interface and its registry.

An arm is one agent setup under test: how many agents, in what shape, with what
handoffs. Every arm implements exactly one method:

    run(task, workspace, referee) -> RunResult

and gets exactly three things: the task to attempt, the sandbox to work in, and
the referee it must run its agents under. It is handed no process handles, no
caps, and no scoring code, so an arm can report on its run but cannot decide
whether the run was any good, and cannot keep an agent alive past a verdict.

Adding an arm means dropping a module into agent_lab/arms/ with @register on
the class. runner.py, referee.py and scorer.py never learn its name.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..agentproc import AgentProcessResult
from ..config import Config
from ..referee import Referee
from ..sandbox import Workspace


@dataclass(frozen=True)
class Task:
    """One benchmark task, already rendered into the frozen prompt."""

    level: str
    prompt: str
    task_text: str
    expected_files: int
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    """What an arm reports about its own run.

    Everything here is the arm's account of itself, which is why none of it is
    scored. The referee's ledger is the record of what happened; the scorer
    judges the artifact. If the two disagree, run.json shows both.
    """

    deploy_url: str | None = None
    agent_reported_done: bool = False
    notes: str = ""
    processes: list[AgentProcessResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deploy_url": self.deploy_url,
            "agent_reported_done": self.agent_reported_done,
            "notes": self.notes,
            "processes": [p.to_dict() for p in self.processes],
        }


class Arm(ABC):
    """Base class for every arm."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""

    def __init__(self, config: Config) -> None:
        self.config = config

    @abstractmethod
    def run(self, task: Task, workspace: Workspace, referee: Referee) -> RunResult:
        """Attempt `task` in `workspace`, running every agent under `referee`."""


_REGISTRY: dict[str, type[Arm]] = {}


def register(cls: type[Arm]) -> type[Arm]:
    """Class decorator that puts an arm on the registry."""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} needs a class-level name")
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        raise ValueError(f"two arms are called {cls.name!r}: {existing.__name__} and {cls.__name__}")
    _REGISTRY[cls.name] = cls
    return cls


def registry() -> dict[str, type[Arm]]:
    return dict(_REGISTRY)
