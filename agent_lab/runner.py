"""The runner: start a run, hold the referee, write the record.

The runner knows three things about arms: their names, that they take
(task, workspace, referee), and that they return a RunResult. It never imports
an arm module directly - arms are discovered from the filesystem - so adding
arms/arm2.py changes nothing here.

Order of business:
  1. preflight (credentials, sandbox, config) - refuse to start if anything
     is wrong, before any agent exists;
  2. build the workspace and the referee;
  3. hand them to the arm;
  4. write run.json from the referee's ledger, not the arm's account;
  5. score, then write the morning log.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import deploy, logtool, notify, scorer
from .arms import available, describe, get_arm
from .arms.base import RunResult, Task
from .config import REPO_ROOT, Config
from .redact import Redactor
from .referee import Referee
from .sandbox import RUNS_ROOT, SandboxError, Workspace, new_run_id

BENCHMARKS = REPO_ROOT / "benchmarks"
PROMPT_PATH = BENCHMARKS / "prompt.txt"
TASKS_PATH = BENCHMARKS / "tasks.json"


class PreflightError(RuntimeError):
    """A run that must not start."""


# -- preflight -------------------------------------------------------------


def check_credentials(config: Config, env: dict[str, str] | None = None) -> None:
    """Hard-fail if a required credential is missing.

    Credentials come from the environment and go nowhere else: not to disk, not
    into the prompt, not into a transcript.
    """
    source = dict(os.environ if env is None else env)
    missing = [name for name in config.agent.grant_credentials if not source.get(name)]
    if missing:
        raise PreflightError(
            "missing credentials: " + ", ".join(missing) + ". "
            "Export them and try again; agent-lab will not start a run without them."
        )


def check_prompt_is_clean(prompt: str, config: Config) -> None:
    """The prompt must not carry caps or credentials into the agent.

    What actually guarantees this is structural: the prompt is prompt.txt with
    {TASK} substituted and nothing else, and config is never handed to the
    template. These checks are belt and braces on top of that. A bare two-digit
    cap cannot be detected by value without false-positiving on ordinary task
    text, so cap names are checked instead.
    """
    redactor = Redactor.from_env()
    if redactor.holds_secret(prompt):
        raise PreflightError("the rendered prompt contains something that looks like a credential")

    lowered = prompt.lower()
    for cap_name, cap_value in config.referee.caps_summary().items():
        if cap_name in lowered:
            raise PreflightError(f"the prompt names the referee cap {cap_name}; caps stay in config.toml")
        if len(str(cap_value)) > 2 and str(cap_value) in prompt:
            raise PreflightError(
                f"the prompt appears to contain the referee cap {cap_name}={cap_value}; caps stay in config.toml"
            )


def harness_digest() -> str:
    """A fingerprint of the harness, to detect an agent editing the referee."""
    digest = hashlib.sha256()
    targets = sorted(
        [p for p in (REPO_ROOT / "agent_lab").rglob("*.py") if "__pycache__" not in p.parts]
        + [p for p in BENCHMARKS.rglob("*") if p.is_file()]
    )
    for path in targets:
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


# -- task loading ----------------------------------------------------------


def load_task(level: str, extra: dict[str, Any] | None = None) -> Task:
    tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    if level not in tasks or level.startswith("_"):
        known = ", ".join(k for k in tasks if not k.startswith("_"))
        raise PreflightError(f"unknown level {level!r}; known levels: {known}")
    spec = tasks[level]
    template = PROMPT_PATH.read_text(encoding="utf-8")
    if "{TASK}" not in template:
        raise PreflightError("benchmarks/prompt.txt has no {TASK} placeholder")
    return Task(
        level=level,
        prompt=template.replace("{TASK}", spec["task"]),
        task_text=spec["task"],
        expected_files=int(spec["expected_files"]),
        extra=dict(extra or {}),
    )


# -- the run ---------------------------------------------------------------


def execute_run(
    level: str,
    arm_name: str,
    config: Config,
    extra: dict[str, Any] | None = None,
    runs_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run one arm against one task. Returns (run_dir, record)."""
    check_credentials(config)
    arm_cls = get_arm(arm_name)
    task = load_task(level, extra)
    check_prompt_is_clean(task.prompt, config)

    run_id = new_run_id(level, arm_name)
    workspace = Workspace.create(run_id, runs_root)  # raises SandboxError outside runs/

    # The harness owns deployment identity. Doing this before the agent exists
    # means the arm cannot choose where its work is published, and a failure
    # here stops the run rather than producing a result nobody can trace.
    deploy_target = None
    if config.deploy.manage_projects:
        try:
            deploy_target = deploy.prepare_target(
                workspace, arm_name, level, config.deploy, os.environ.get("VERCEL_TOKEN", "")
            )
        except deploy.DeployError as exc:
            raise PreflightError(str(exc)) from exc

    # Caps are per level: hard gets a longer clock than easy or medium.
    referee = Referee(config.referee.for_level(level), workspace=workspace.path)
    workspace.git_init()

    started = datetime.now(timezone.utc)
    digest_before = harness_digest()

    arm = arm_cls(config)
    outcome_detail = ""
    error: str | None = None
    try:
        referee.start()
        result = arm.run(task, workspace, referee)
    except Exception:
        error = traceback.format_exc(limit=6)
        result = RunResult(notes="the arm raised before it finished")
    ended = datetime.now(timezone.utc)

    verdict = referee.verdict
    if error is not None:
        outcome = "arm_error"
        outcome_detail = (error.strip().splitlines() or ["arm raised"])[-1]
    elif verdict is not None:
        outcome = verdict.condition.value
        outcome_detail = verdict.detail
    elif referee.observed == 0:
        # The arm never ran anything the referee could see. Not a result.
        outcome = "arm_invalid"
        outcome_detail = "the referee observed no actions: the arm did not run its agent under it"
    else:
        outcome = "agent_exited"
        outcome_detail = "the agent process ended without a kill condition firing"

    ledger = referee.ledger
    deploy_url = ledger.deploy_urls[-1] if ledger.deploy_urls else None

    record: dict[str, Any] = {
        "run_id": run_id,
        "level": level,
        "arm": arm_name,
        "task": task.task_text,
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ended_at": ended.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "outcome": outcome,
        "outcome_detail": outcome_detail,
        "deploy_url": deploy_url,
        "deploy_target": deploy_target.to_dict() if deploy_target else None,
        "verdict": verdict.to_dict() if verdict else None,
        "ledger": ledger.to_dict(),
        "asked_question": ledger.questions > 0,
        "cost_usd": _total_cost(result.processes),
        "model": next((p.model for p in result.processes if p.model), None),
        "permission_denials": sum(p.permission_denials for p in result.processes),
        "files": {
            "touched": len(ledger.files_written),
            "needed": task.expected_files,
            "written": sorted(ledger.files_written),
            "on_disk": workspace.source_files(),
        },
        "config": {
            "path": str(config.path),
            "digest": config.digest,
            "caps": config.referee.caps_summary(),
        },
        "integrity": {
            "harness_unchanged": harness_digest() == digest_before,
            "config_unchanged": config.still_matches_disk(),
        },
        "arm_result": result.to_dict(),
        "processes": [p.to_dict() for p in result.processes],
        "actions": [a.to_dict() for a in referee.actions],
        "error": error,
    }
    if verdict is None:
        record["last_actions"] = record["actions"][-config.referee.action_window:]

    redactor = Redactor.from_env()
    (workspace.run_dir / "run.json").write_text(
        json.dumps(redactor.obj(record), indent=2) + "\n", encoding="utf-8"
    )
    return workspace.run_dir, record


def _total_cost(processes) -> float | None:
    costs = [p.total_cost_usd for p in processes if isinstance(p.total_cost_usd, (int, float))]
    return round(sum(costs), 4) if costs else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_lab.runner", description="Run one arm against one benchmark task, under the referee."
    )
    parser.add_argument("--level", help="easy, medium or hard")
    parser.add_argument("--arm", help="which arm to run (see --list)")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--runs-root", type=Path, help="override the runs/ directory")
    parser.add_argument("--fake-script", type=Path, help="script for the 'fake' arm")
    parser.add_argument("--url", help="score against this URL instead of the one the run produced")
    parser.add_argument("--no-score", action="store_true", help="skip scoring (the log still gets written)")
    parser.add_argument("--list", action="store_true", help="list the arms the harness can run")
    args = parser.parse_args(argv)

    if args.list:
        for name, description in describe().items():
            print(f"  {name:<10} {description}")
        return 0
    if not args.level or not args.arm:
        parser.error("--level and --arm are required (or use --list)")

    config = Config.load(args.config)
    extra = {"fake_script": str(args.fake_script)} if args.fake_script else {}

    try:
        run_dir, record = execute_run(args.level, args.arm, config, extra, args.runs_root)
    except (PreflightError, SandboxError, KeyError) as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    print(f"run     {record['run_id']}")
    print(f"outcome {record['outcome']}  ({record['outcome_detail']})")

    if not args.no_score:
        url = args.url or record.get("deploy_url")
        report = scorer.score(url, args.level, config)
        (run_dir / "score.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        if url and url != record.get("deploy_url"):
            record["deploy_url"] = url
            (run_dir / "run.json").write_text(
                json.dumps(Redactor.from_env().obj(record), indent=2) + "\n", encoding="utf-8"
            )
        print(f"BUILT   {report.built}   SHIPPED {report.shipped}")

    log = logtool.write_log(run_dir)
    print(f"log     {log}")

    if config.email.enabled:
        score_path = run_dir / "score.json"
        score = json.loads(score_path.read_text(encoding="utf-8")) if score_path.exists() else None
        sent, why = notify.send_report(record, score, log.read_text(encoding="utf-8"), config.email)
        print(f"email   {'sent - ' if sent else 'not sent - '}{why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
