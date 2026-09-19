#!/usr/bin/env python3
"""A scripted stand-in for a coding agent.

It speaks the same `--output-format stream-json` protocol a real agent speaks,
writes real files into its working directory, and takes real time - so the
referee, the supervision loop and the kill path can all be proven against it
before a real agent is ever pointed at the harness.

It is deliberately standalone: no agent_lab imports, so it runs from any
working directory, exactly as an external agent binary would.

Usage:  fakeagent.py --script script.json
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SESSION = str(uuid.uuid4())
_counter = 0


def _emit(obj: dict[str, Any]) -> None:
    obj.setdefault("session_id", SESSION)
    obj.setdefault("uuid", str(uuid.uuid4()))
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _tool_id() -> str:
    global _counter
    _counter += 1
    return f"toolu_fake{_counter:04d}"


def _assistant(blocks: list[dict[str, Any]]) -> None:
    _emit({
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"role": "assistant", "type": "message", "content": blocks},
    })


def _tool_result(tool_id: str, content: str, is_error: bool = False) -> None:
    _emit({
        "type": "user",
        "parent_tool_use_id": None,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}
            ],
        },
    })


def _step(step: dict[str, Any], workdir: Path) -> None:
    kind = step.get("do", "say")

    if kind == "sleep":
        time.sleep(float(step.get("seconds", 1)))
        return

    if kind == "say":
        _assistant([{"type": "text", "text": step.get("text", "working")}])
        return

    if kind in ("ask", "done"):
        # A message with no tool calls: the agent handing the turn back.
        _assistant([{"type": "text", "text": step["text"]}])
        return

    if kind == "ask_tool":
        tool_id = _tool_id()
        _assistant([
            {"type": "text", "text": step.get("text", "I need a decision.")},
            {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion",
             "input": {"questions": [{"question": step.get("text", "which?")}]}},
        ])
        _tool_result(tool_id, "Permission denied: no human is available.", is_error=True)
        return

    if kind == "write":
        path = step.get("path", "index.html")
        target = workdir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(step.get("content", "<!doctype html><h1>fake</h1>\n"), encoding="utf-8")
        tool_id = _tool_id()
        _assistant([
            {"type": "text", "text": step.get("text", f"Writing {path}.")},
            {"type": "tool_use", "id": tool_id, "name": "Write",
             "input": {"file_path": str(target), "content": step.get("content", "")}},
        ])
        _tool_result(tool_id, f"File created successfully at: {target}")
        return

    if kind in ("bash", "deploy"):
        command = step.get("command", "npx vercel --prod" if kind == "deploy" else "ls")
        tool_id = _tool_id()
        _assistant([{"type": "tool_use", "id": tool_id, "name": "Bash",
                     "input": {"command": command, "description": step.get("text", "")}}])
        if kind == "deploy" and step.get("ok", True):
            url = step.get("url", "https://fake-deploy.vercel.app")
            _tool_result(tool_id, f"Inspect: https://vercel.com/x/y\nProduction: {url} [2s]")
        else:
            _tool_result(tool_id, step.get("result", "ok"), is_error=bool(step.get("error")))
        return

    raise SystemExit(f"fakeagent: unknown step {kind!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scripted fake coding agent")
    parser.add_argument("--script", required=True, help="path to the script JSON")
    args, _unknown = parser.parse_known_args()

    script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    workdir = Path.cwd()

    if script.get("stubborn"):
        # Ignore SIGTERM, so the harness has to escalate to SIGKILL.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    _emit({"type": "system", "subtype": "init", "cwd": str(workdir),
           "model": "fake-agent", "permissionMode": "acceptEdits", "tools": ["Write", "Bash"]})

    for step in script.get("steps", []):
        for _ in range(int(step.get("repeat", 1))):
            _step(step, workdir)

    final = script.get("final", "success")
    if final == "hang":
        while True:
            time.sleep(0.2)

    _emit({
        "type": "result",
        "subtype": final,
        "is_error": final != "success",
        "num_turns": _counter,
        "duration_ms": 0,
        "result": script.get("result_text", "fake run finished"),
        "total_cost_usd": 0.0,
        "permission_denials": [],
    })
    return int(script.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
