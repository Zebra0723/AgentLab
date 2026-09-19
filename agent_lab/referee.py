"""The referee: kill conditions, loop detection, budget accounting.

The referee runs in the parent process. It is fed `Action` records derived from
an agent process's own protocol stream, and it decides - alone - when a run
ends. Three properties make it hard for an agent to influence:

* Its caps come from config.toml, which is never shown to an agent and whose
  hash is recorded with the run.
* It is fed normalized Actions, never agent-authored text asking for anything.
  There is no Action an agent can emit that raises a cap.
* It has no setters. Once a verdict fires, the ledger freezes and the verdict
  never changes, whatever arrives afterwards.

An arm receives a Referee but can only pass it along and read it. The runner
checks afterwards that the referee actually saw the run.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .config import RefereeConfig
from .events import Action, ActionKind


class KillCondition(str, Enum):
    """The complete set of reasons a run ends. Nothing else stops a run."""

    MESSAGE_CAP = "message_cap"
    WALL_CLOCK = "wall_clock"
    DEPLOY_CAP = "deploy_cap"
    SILENT_LOOP = "silent_loop"
    QUESTION_LOOP = "question_loop"
    AGENT_DONE = "agent_done"


@dataclass(frozen=True)
class Verdict:
    """Why the run ended, and what led up to it."""

    condition: KillCondition
    detail: str
    at_elapsed: float
    last_actions: tuple[Action, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition.value,
            "detail": self.detail,
            "at_elapsed": round(self.at_elapsed, 3),
            "last_actions": [a.to_dict() for a in self.last_actions],
        }


@dataclass
class Ledger:
    """Budget accounting. Frozen at the moment a verdict fires."""

    messages: int = 0
    subagent_messages: int = 0
    tool_calls: int = 0
    writes: int = 0
    deploys: int = 0
    deploy_successes: int = 0
    questions: int = 0
    files_written: Counter[str] = field(default_factory=Counter)
    deploy_urls: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "subagent_messages": self.subagent_messages,
            "tool_calls": self.tool_calls,
            "writes": self.writes,
            "deploys": self.deploys,
            "deploy_successes": self.deploy_successes,
            "questions": self.questions,
            "wall_clock_seconds": round(self.elapsed, 3),
            "files_written": dict(self.files_written),
            "deploy_urls": list(self.deploy_urls),
        }


class Referee:
    def __init__(
        self,
        config: RefereeConfig,
        workspace: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._workspace = Path(workspace).resolve() if workspace else None
        self._clock = clock
        self._started: float | None = None
        self._verdict: Verdict | None = None
        self._ledger = Ledger()
        self._actions: list[Action] = []
        self._window: deque[Action] = deque(maxlen=max(1, config.action_window))
        # Writes since the last deploy attempt, per file. Cleared on every deploy.
        self._writes_since_deploy: Counter[str] = Counter()
        self._observed = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._started is None:
            self._started = self._clock()

    @property
    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return self._clock() - self._started

    @property
    def verdict(self) -> Verdict | None:
        return self._verdict

    @property
    def should_stop(self) -> bool:
        return self._verdict is not None

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    @property
    def actions(self) -> list[Action]:
        return list(self._actions)

    @property
    def observed(self) -> int:
        """How many actions the referee was fed. The runner checks this is > 0."""
        return self._observed

    @property
    def caps(self) -> RefereeConfig:
        return self._config

    # -- the only two inputs ----------------------------------------------

    def observe(self, action: Action) -> Verdict | None:
        """Record an action and decide whether the run ends here."""
        self.start()
        self._observed += 1
        self._actions.append(action)
        if self._verdict is not None:
            # Already decided. Keep the action for the record, change nothing.
            return self._verdict
        self._window.append(action)
        self._count(action)
        self._ledger.elapsed = self.elapsed
        return self._evaluate(action)

    def tick(self) -> Verdict | None:
        """Check the conditions that pass with time rather than with actions."""
        self.start()
        if self._verdict is not None:
            return self._verdict
        elapsed = self.elapsed
        self._ledger.elapsed = elapsed
        if elapsed >= self._config.wall_clock_seconds:
            return self._fire(
                KillCondition.WALL_CLOCK,
                f"wall clock {elapsed:.0f}s >= cap {self._config.wall_clock_seconds:.0f}s",
                elapsed,
            )
        return None

    # -- internals ---------------------------------------------------------

    def _count(self, action: Action) -> None:
        led = self._ledger
        if action.kind is ActionKind.MESSAGE:
            # Every agent's messages come out of one budget, so a subagent's
            # message counts toward the cap as well as being counted separately.
            led.messages += 1
            if action.subagent:
                led.subagent_messages += 1
        elif action.kind is ActionKind.TOOL:
            led.tool_calls += 1
        elif action.kind is ActionKind.WRITE:
            led.writes += 1
            led.tool_calls += 1
            key = self._normalize(action.path)
            led.files_written[key] += 1
            self._writes_since_deploy[key] += 1
        elif action.kind is ActionKind.DEPLOY:
            led.deploys += 1
            led.tool_calls += 1
            # A deploy resets loop detection: work that reaches a deploy is not
            # a silent loop, whether or not the deploy succeeded.
            self._writes_since_deploy.clear()
        elif action.kind is ActionKind.DEPLOY_OK:
            led.deploy_successes += 1
            if action.url:
                led.deploy_urls.append(action.url)
        elif action.kind is ActionKind.QUESTION:
            led.questions += 1

    def _evaluate(self, action: Action) -> Verdict | None:
        cfg = self._config
        led = self._ledger
        elapsed = self.elapsed

        if action.kind is ActionKind.DONE:
            return self._fire(KillCondition.AGENT_DONE, f"agent reported done: {action.detail}", elapsed)

        if led.messages >= cfg.message_cap:
            return self._fire(
                KillCondition.MESSAGE_CAP, f"messages {led.messages} >= cap {cfg.message_cap}", elapsed
            )

        if led.deploys >= cfg.deploy_cap:
            return self._fire(
                KillCondition.DEPLOY_CAP, f"deploys {led.deploys} >= cap {cfg.deploy_cap}", elapsed
            )

        if led.questions >= cfg.question_cap:
            return self._fire(
                KillCondition.QUESTION_LOOP,
                f"asked the human {led.questions} times (cap {cfg.question_cap})",
                elapsed,
            )

        if action.kind is ActionKind.WRITE:
            key = self._normalize(action.path)
            count = self._writes_since_deploy[key]
            if count >= cfg.silent_loop_writes:
                return self._fire(
                    KillCondition.SILENT_LOOP,
                    f"wrote {key} {count} times with no deploy in between (cap {cfg.silent_loop_writes})",
                    elapsed,
                )

        # Time can pass between actions, so check it on every action too.
        if elapsed >= cfg.wall_clock_seconds:
            return self._fire(
                KillCondition.WALL_CLOCK,
                f"wall clock {elapsed:.0f}s >= cap {cfg.wall_clock_seconds:.0f}s",
                elapsed,
            )
        return None

    def _fire(self, condition: KillCondition, detail: str, elapsed: float) -> Verdict:
        self._ledger.elapsed = elapsed
        self._verdict = Verdict(
            condition=condition,
            detail=detail,
            at_elapsed=elapsed,
            last_actions=tuple(self._window),
        )
        return self._verdict

    def _normalize(self, path: str | None) -> str:
        """One file, one key - however the agent chose to spell the path."""
        if not path:
            return "<unknown>"
        p = Path(path)
        if self._workspace is not None:
            if not p.is_absolute():
                p = self._workspace / p
            try:
                return str(p.resolve().relative_to(self._workspace))
            except (ValueError, OSError):
                return str(p)
        return str(p)

    # -- output ------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        return {
            "caps": self._config.caps_summary(),
            "verdict": self._verdict.to_dict() if self._verdict else None,
            "ledger": self._ledger.to_dict(),
            "actions_observed": self._observed,
        }
