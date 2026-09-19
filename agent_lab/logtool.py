"""The morning log: one screen of plain text per run.

Written to runs/<id>/log.txt from run.json and score.json, so it can be
regenerated at any time without re-running anything. No colour codes, no
escape sequences, fixed-width columns, and the things you want first at the
top: what it was, how it ended, whether it built, whether it shipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .sandbox import RUNS_ROOT

WIDTH = 72
RULE = "=" * WIDTH
THIN = "-" * WIDTH


def _hms(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--:--"
    total = int(float(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _plain(text: Any, width: int = WIDTH) -> str:
    """No control characters reach the log, whatever the agent wrote."""
    s = "" if text is None else str(text)
    s = "".join(ch if ch == " " or ch.isprintable() else " " for ch in s)
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 1] + "~"


def render(record: dict[str, Any], score: dict[str, Any] | None) -> str:
    lines: list[str] = []
    add = lines.append

    ledger = record.get("ledger") or {}
    caps = (record.get("config") or {}).get("caps") or {}
    verdict = record.get("verdict") or {}
    files = record.get("files") or {}
    metrics = (score or {}).get("metrics") or {}

    add(RULE)
    add(f" agent-lab   {record.get('run_id', '?')}")
    add(RULE)
    add(f" level    {_plain(record.get('level'), 20):<22} arm      {_plain(record.get('arm'), 20)}")
    add(f" outcome  {_plain(str(record.get('outcome', '?')).upper(), 20):<22} started  {_plain(record.get('started_at'))}")
    add(f" built    {_plain((score or {}).get('built', 'UNSCORED'), 20):<22} shipped  {_plain((score or {}).get('shipped', 'UNSCORED'))}")
    add("")

    # -- BUILT: hidden tests, line by line --------------------------------
    built = (score or {}).get("built", "UNSCORED")
    add(f"HIDDEN TESTS   BUILT: {built}   {_plain((score or {}).get('built_detail', 'not scored'), 40)}")
    checks = (score or {}).get("checks") or []
    for check in checks:
        add(f"  {check.get('status', '?'):<5} {_plain(check.get('name'), 24):<24} {_plain(check.get('detail'), 36)}")
    for prompt in ((score or {}).get("manual_prompts") or []):
        add(f"  {_plain(prompt, WIDTH - 2)}")
    if not checks and not ((score or {}).get("manual_prompts")):
        add("  (no hidden tests were run)")
    add("")

    # -- SHIPPED ----------------------------------------------------------
    shipped = (score or {}).get("shipped", "UNSCORED")
    add(f"SHIPPED: {shipped}   {_plain((score or {}).get('shipped_detail', 'not scored'), 50)}")
    add(f"  url    {_plain(record.get('deploy_url') or '(none)', 60)}")
    add("")

    # -- instruction following, mechanical --------------------------------
    add("INSTRUCTION FOLLOWING")
    asked = record.get("asked_question")
    add(f"  asked a question     {'YES' if asked else 'no'}  ({ledger.get('questions', 0)} time(s))")
    emoji = metrics.get("emoji_count")
    add(f"  emoji in shipped UI  {emoji if emoji is not None else '(not measured)'}")
    add(f"  files touched        {files.get('touched', 0)} of {files.get('needed', '?')} needed")
    add(f"  messages             {ledger.get('messages', 0)} of {caps.get('message_cap', '?')}")
    add(
        f"  deploys              {ledger.get('deploys', 0)} of {caps.get('deploy_cap', '?')}"
        f"   ({ledger.get('deploy_successes', 0)} succeeded)"
    )
    add(f"  wall clock           {_hms(ledger.get('wall_clock_seconds'))} of {_hms(caps.get('wall_clock_seconds'))}")
    denials = record.get("permission_denials") or 0
    if denials:
        add(f"  tool calls REFUSED   {denials}  (the harness blocked the agent this often)")
    add("")

    # -- last actions ------------------------------------------------------
    last = verdict.get("last_actions") or record.get("last_actions") or []
    add(f"LAST {len(last)} ACTIONS")
    if not last:
        add("  (none recorded)")
    for i, action in enumerate(last):
        fired = i == len(last) - 1 and bool(verdict)
        line = f"  [{float(action.get('elapsed', 0)):7.1f}s] {_plain(action.get('kind'), 9):<9} {_plain(action.get('detail'), 40)}"
        add(f"{line:<63}<<" if fired else line)
    add("")

    # -- where it died -----------------------------------------------------
    add(THIN)
    if verdict:
        add(f"DIED: {verdict.get('condition', '?')} at {_hms(verdict.get('at_elapsed'))}")
        add(f"      {_plain(verdict.get('detail'), WIDTH - 8)}")
    else:
        add(f"DIED: no kill condition fired - {_plain(record.get('outcome_detail') or record.get('outcome'), 40)}")
    integrity = record.get("integrity") or {}
    if integrity and not (integrity.get("harness_unchanged", True) and integrity.get("config_unchanged", True)):
        add("WARNING: harness or config changed during this run - treat the result as void")
    add(THIN)
    for process in record.get("processes") or []:
        add(
            f"  {_plain(process.get('label'), 10):<10} exit={str(process.get('exit_code')):<5}"
            f" signalled={str(bool(process.get('killed'))):<5} {process.get('transcript')} "
            f"({process.get('transcript_lines', 0)} lines)"
        )
    add(RULE)
    return "\n".join(lines) + "\n"


def write_log(run_dir: Path) -> Path:
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    score_path = run_dir / "score.json"
    score = json.loads(score_path.read_text(encoding="utf-8")) if score_path.exists() else None
    out = run_dir / "log.txt"
    out.write_text(render(record, score), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_lab.logtool", description="Write runs/<id>/log.txt")
    parser.add_argument("--run", required=True, help="run id, or a path to runs/<id>")
    parser.add_argument("--print", action="store_true", help="also print the log")
    args = parser.parse_args(argv)

    run_dir = Path(args.run) if Path(args.run).is_dir() else RUNS_ROOT / args.run
    if not (run_dir / "run.json").exists():
        print(f"no run.json in {run_dir}", file=sys.stderr)
        return 1
    out = write_log(run_dir)
    if args.print:
        sys.stdout.write(out.read_text(encoding="utf-8"))
    else:
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
