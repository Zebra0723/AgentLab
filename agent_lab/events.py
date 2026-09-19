"""Normalized actions, and the parser that derives them from a transcript stream.

The referee never looks at raw agent output. It sees only `Action` records
produced here, from the live stdout pipe of an agent process. That is the seam
that makes the referee hard to influence: an agent can write whatever it likes
into files, but it cannot synthesize an Action, because Actions come from the
process's own protocol stream, not from its content.

The stream format is Claude Code's `--output-format stream-json`: one JSON
object per line, with `type` in {system, assistant, user, result, ...}.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .config import DetectConfig


class ActionKind(str, Enum):
    MESSAGE = "message"        # one assistant message
    TOOL = "tool"              # a tool call that is not a write or a deploy
    WRITE = "write"            # a file write
    DEPLOY = "deploy"          # a deploy attempt
    DEPLOY_OK = "deploy_ok"    # a deploy attempt that returned a URL
    QUESTION = "question"      # the agent asked the human something
    DONE = "done"              # the agent said it was finished
    EXIT = "exit"              # the agent process ended on its own
    ERROR = "error"            # the harness could not make sense of a line


@dataclass(frozen=True)
class Action:
    seq: int
    elapsed: float
    kind: ActionKind
    detail: str
    agent: str = "agent"
    path: str | None = None
    url: str | None = None
    subagent: bool = False

    def render(self) -> str:
        """One fixed-width line for the morning log."""
        return f"[{self.elapsed:7.1f}s] {self.kind.value:<9} {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        d = {
            "seq": self.seq,
            "elapsed": round(self.elapsed, 3),
            "kind": self.kind.value,
            "detail": self.detail,
            "agent": self.agent,
        }
        if self.path:
            d["path"] = self.path
        if self.url:
            d["url"] = self.url
        if self.subagent:
            d["subagent"] = True
        return d


def _result_text(block: Any) -> str:
    """Flatten a tool_result's content, which may be a string or a block list."""
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        return "\n".join(_result_text(b) for b in block)
    if isinstance(block, dict):
        if "text" in block:
            return str(block["text"])
        if "content" in block:
            return _result_text(block["content"])
    return ""


@dataclass
class _PendingTool:
    """A tool call seen on the stream, waiting for its result."""

    name: str
    kind: ActionKind
    detail: str
    path: str | None = None


class TranscriptParser:
    """Turns raw stream-json objects into Actions. One per agent process."""

    def __init__(self, detect: DetectConfig, agent: str = "agent") -> None:
        self.detect = detect
        self.agent = agent
        self._seq = 0
        self._pending: dict[str, _PendingTool] = {}

    def _next(self, elapsed: float, kind: ActionKind, detail: str, **kw: Any) -> Action:
        self._seq += 1
        return Action(seq=self._seq, elapsed=elapsed, kind=kind, detail=detail, agent=self.agent, **kw)

    def feed(self, obj: dict[str, Any], elapsed: float) -> list[Action]:
        kind = obj.get("type")
        if kind == "assistant":
            return self._feed_assistant(obj, elapsed)
        if kind == "user":
            return self._feed_user(obj, elapsed)
        if kind == "result":
            return self._feed_result(obj, elapsed)
        return []

    def _feed_assistant(self, obj: dict[str, Any], elapsed: float) -> list[Action]:
        message = obj.get("message") or {}
        blocks = message.get("content") or []
        if not isinstance(blocks, list):
            blocks = []
        is_subagent = obj.get("parent_tool_use_id") is not None

        tool_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        text_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(str(b.get("text", "")) for b in text_blocks).strip()

        actions: list[Action] = [
            self._next(
                elapsed,
                ActionKind.MESSAGE,
                _summarize(text) if text else f"{len(tool_blocks)} tool call(s)",
                subagent=is_subagent,
            )
        ]

        for block in tool_blocks:
            actions.append(self._classify_tool(block, elapsed))

        # One message asking twice is still one ask: keep at most one question
        # per message so the question loop counts asks, not phrasings.
        if sum(1 for a in actions if a.kind is ActionKind.QUESTION) > 1:
            seen = False
            kept = []
            for a in actions:
                if a.kind is ActionKind.QUESTION:
                    if seen:
                        continue
                    seen = True
                kept.append(a)
            actions = kept

        # A message that stops without calling a tool is the agent handing the
        # turn back. That is where a question, or a report of being done, lands.
        if not tool_blocks and text:
            if self.detect.question_text.search(text):
                actions.append(self._next(elapsed, ActionKind.QUESTION, _summarize(text)))
            if self.detect.done_text.search(text):
                actions.append(self._next(elapsed, ActionKind.DONE, _summarize(text)))
        return actions

    def _classify_tool(self, block: dict[str, Any], elapsed: float) -> Action:
        name = str(block.get("name", "?"))
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_id = str(block.get("id", ""))

        if name in self.detect.question_tools:
            action = self._next(elapsed, ActionKind.QUESTION, f"{name} tool")
        elif name in self.detect.write_tools:
            path = str(tool_input.get(self.detect.write_tools[name], "") or "")
            action = self._next(elapsed, ActionKind.WRITE, f"{name} {path}", path=path)
        elif name == "Bash":
            command = str(tool_input.get("command", "") or "")
            if any(p.search(command) for p in self.detect.deploy_commands):
                action = self._next(elapsed, ActionKind.DEPLOY, _summarize(command, 70))
            else:
                action = self._next(elapsed, ActionKind.TOOL, f"Bash {_summarize(command, 60)}")
        else:
            action = self._next(elapsed, ActionKind.TOOL, name)

        if tool_id:
            self._pending[tool_id] = _PendingTool(name=name, kind=action.kind, detail=action.detail, path=action.path)
        return action

    def _feed_user(self, obj: dict[str, Any], elapsed: float) -> list[Action]:
        message = obj.get("message") or {}
        blocks = message.get("content") or []
        if not isinstance(blocks, list):
            return []
        actions: list[Action] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            pending = self._pending.pop(str(block.get("tool_use_id", "")), None)
            if pending is None or pending.kind is not ActionKind.DEPLOY:
                continue
            text = _result_text(block.get("content"))
            url = self.find_url(text)
            failed = bool(block.get("is_error"))
            if url and not failed:
                actions.append(self._next(elapsed, ActionKind.DEPLOY_OK, url, url=url))
        return actions

    def _feed_result(self, obj: dict[str, Any], elapsed: float) -> list[Action]:
        subtype = str(obj.get("subtype", "unknown"))
        actions = [self._next(elapsed, ActionKind.EXIT, f"agent process ended ({subtype})")]
        # A clean exit is the agent reporting it is finished.
        if subtype == "success" and not obj.get("is_error"):
            actions.append(self._next(elapsed, ActionKind.DONE, "agent process exited cleanly"))
        return actions

    def find_url(self, text: str) -> str | None:
        for pattern in self.detect.deploy_urls:
            match = pattern.search(text or "")
            if match:
                return match.group(0).rstrip(".,;:'\")")
        return None


def _summarize(text: str, width: int = 80) -> str:
    """One line, no control characters, bounded width - the log must stay plain."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\r", " ")).strip()
    flat = "".join(ch for ch in flat if ch == " " or ch.isprintable())
    return flat if len(flat) <= width else flat[: width - 1] + "…"
