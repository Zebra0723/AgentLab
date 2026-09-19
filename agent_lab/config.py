"""Frozen configuration, loaded from config.toml.

The referee's caps live here and nowhere else. They are never rendered into a
prompt and never handed to an agent process. `Config.digest` is recorded in
run.json at the start of a run and re-verified at the end, so a run whose
config changed mid-flight is visible in the record.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"


def _compile_all(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


@dataclass(frozen=True)
class DetectConfig:
    """How the referee recognises an action in a transcript stream."""

    deploy_commands: tuple[re.Pattern[str], ...]
    deploy_urls: tuple[re.Pattern[str], ...]
    write_tools: dict[str, str]
    question_tools: frozenset[str]
    question_text: re.Pattern[str]
    done_text: re.Pattern[str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DetectConfig":
        return cls(
            deploy_commands=_compile_all(d["deploy_commands"]),
            deploy_urls=_compile_all(d["deploy_urls"]),
            write_tools=dict(d["write_tools"]),
            question_tools=frozenset(d["question_tools"]),
            question_text=re.compile(d["question_text"]),
            done_text=re.compile(d["done_text"]),
        )


@dataclass(frozen=True)
class RefereeConfig:
    message_cap: int
    wall_clock_seconds: float
    deploy_cap: int
    silent_loop_writes: int
    question_cap: int
    action_window: int
    kill_grace_seconds: float
    tick_seconds: float
    detect: DetectConfig

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RefereeConfig":
        return cls(
            message_cap=int(d["message_cap"]),
            wall_clock_seconds=float(d["wall_clock_seconds"]),
            deploy_cap=int(d["deploy_cap"]),
            silent_loop_writes=int(d["silent_loop_writes"]),
            question_cap=int(d["question_cap"]),
            action_window=int(d["action_window"]),
            kill_grace_seconds=float(d["kill_grace_seconds"]),
            tick_seconds=float(d["tick_seconds"]),
            detect=DetectConfig.from_dict(d["detect"]),
        )

    def caps_summary(self) -> dict[str, Any]:
        return {
            "message_cap": self.message_cap,
            "wall_clock_seconds": self.wall_clock_seconds,
            "deploy_cap": self.deploy_cap,
            "silent_loop_writes": self.silent_loop_writes,
            "question_cap": self.question_cap,
        }


@dataclass(frozen=True)
class AgentConfig:
    binary: str
    model: str
    permission_mode: str
    allowed_tools: tuple[str, ...]
    max_turns: int
    include_partial_messages: bool
    bare: bool
    extra_args: tuple[str, ...]
    env_passthrough: tuple[str, ...]
    grant_credentials: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentConfig":
        return cls(
            binary=d["binary"],
            model=d["model"],
            permission_mode=d["permission_mode"],
            allowed_tools=tuple(d.get("allowed_tools", ())),
            max_turns=int(d["max_turns"]),
            include_partial_messages=bool(d["include_partial_messages"]),
            bare=bool(d.get("bare", False)),
            extra_args=tuple(d.get("extra_args", ())),
            env_passthrough=tuple(d["env_passthrough"]),
            grant_credentials=tuple(d["grant_credentials"]),
        )


@dataclass(frozen=True)
class ScorerConfig:
    http_timeout_seconds: float
    check_timeout_ms: int
    viewport_width: int
    viewport_height: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScorerConfig":
        return cls(
            http_timeout_seconds=float(d["http_timeout_seconds"]),
            check_timeout_ms=int(d["check_timeout_ms"]),
            viewport_width=int(d["viewport_width"]),
            viewport_height=int(d["viewport_height"]),
        )


@dataclass(frozen=True)
class Config:
    path: Path
    digest: str
    referee: RefereeConfig
    agent: AgentConfig
    scorer: ScorerConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        blob = cfg_path.read_bytes()
        data = tomllib.loads(blob.decode("utf-8"))
        return cls(
            path=cfg_path.resolve(),
            digest=hashlib.sha256(blob).hexdigest(),
            referee=RefereeConfig.from_dict(data["referee"]),
            agent=AgentConfig.from_dict(data["agent"]),
            scorer=ScorerConfig.from_dict(data["scorer"]),
            raw=data,
        )

    def still_matches_disk(self) -> bool:
        """False if config.toml changed since this Config was loaded."""
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest() == self.digest
        except OSError:
            return False
