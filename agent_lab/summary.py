"""One table for the whole experiment.

Reading fifty log files by hand is how an experiment dies. This walks runs/ and
reports the four things worth comparing across arms:

    1. ship rate        - how often the thing ended up live
    2. hidden tests     - how much of it actually worked, as a score not a verdict
    3. cost             - messages spent getting there
    4. instruction-following - emoji in the UI, and whether it asked a question

It deliberately does not rank arms or compute a single score. An arm that
ships every time with half the tests passing and an arm that ships half the
time perfectly are different animals, and flattening them into one number
throws away the finding.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .sandbox import RUNS_ROOT


@dataclass
class RunRow:
    run_id: str
    arm: str
    level: str
    outcome: str
    built: str
    shipped: str
    checks_passed: int
    checks_total: int
    messages: int
    deploys: int
    wall_clock: float
    emoji: int | None
    asked: bool
    files_touched: int
    files_needed: int
    cost_usd: float | None
    model: str

    @property
    def tests(self) -> str:
        return f"{self.checks_passed}/{self.checks_total}" if self.checks_total else "-"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "arm": self.arm, "level": self.level, "outcome": self.outcome,
            "built": self.built, "shipped": self.shipped,
            "checks_passed": self.checks_passed, "checks_total": self.checks_total,
            "messages": self.messages, "deploys": self.deploys,
            "wall_clock_seconds": round(self.wall_clock, 1),
            "emoji": self.emoji, "asked_question": self.asked,
            "files_touched": self.files_touched, "files_needed": self.files_needed,
            "cost_usd": self.cost_usd, "model": self.model,
        }


def load_rows(runs_root: Path | None = None) -> list[RunRow]:
    root = runs_root or RUNS_ROOT
    rows: list[RunRow] = []
    for record_path in sorted(root.glob("*/run.json")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        score_path = record_path.parent / "score.json"
        score: dict[str, Any] = {}
        if score_path.exists():
            try:
                score = json.loads(score_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                score = {}

        checks = score.get("checks") or []
        ledger = record.get("ledger") or {}
        files = record.get("files") or {}
        processes = record.get("processes") or []
        costs = [p.get("total_cost_usd") for p in processes if isinstance(p.get("total_cost_usd"), (int, float))]
        models = [p.get("model") for p in processes if p.get("model")]

        rows.append(RunRow(
            run_id=record.get("run_id", record_path.parent.name),
            arm=record.get("arm", "?"),
            level=record.get("level", "?"),
            outcome=record.get("outcome", "?"),
            built=score.get("built", "UNSCORED"),
            shipped=score.get("shipped", "UNSCORED"),
            checks_passed=sum(1 for c in checks if c.get("status") == "PASS"),
            checks_total=len(checks),
            messages=int(ledger.get("messages", 0)),
            deploys=int(ledger.get("deploys", 0)),
            wall_clock=float(ledger.get("wall_clock_seconds", 0) or 0),
            emoji=(score.get("metrics") or {}).get("emoji_count"),
            asked=bool(record.get("asked_question")),
            files_touched=int(files.get("touched", 0)),
            files_needed=int(files.get("needed", 0)),
            cost_usd=round(sum(costs), 4) if costs else None,
            model=models[0] if models else "",
        ))
    return rows


@dataclass
class Cell:
    """One arm on one level, over however many repeats it has."""

    arm: str
    level: str
    rows: list[RunRow] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def ship_rate(self) -> float:
        return 100.0 * sum(1 for r in self.rows if r.shipped == "PASS") / self.n if self.n else 0.0

    @property
    def mean_tests(self) -> float:
        scored = [r.checks_passed for r in self.rows if r.checks_total]
        return statistics.fmean(scored) if scored else 0.0

    @property
    def tests_total(self) -> int:
        totals = {r.checks_total for r in self.rows if r.checks_total}
        return max(totals) if totals else 0

    @property
    def mean_messages(self) -> float:
        return statistics.fmean([r.messages for r in self.rows]) if self.n else 0.0

    @property
    def mean_emoji(self) -> float | None:
        seen = [r.emoji for r in self.rows if isinstance(r.emoji, int)]
        return statistics.fmean(seen) if seen else None

    @property
    def asked_count(self) -> int:
        return sum(1 for r in self.rows if r.asked)


def cells(rows: Iterable[RunRow]) -> list[Cell]:
    grouped: dict[tuple[str, str], Cell] = {}
    for row in rows:
        key = (row.arm, row.level)
        grouped.setdefault(key, Cell(arm=row.arm, level=row.level)).rows.append(row)
    return [grouped[k] for k in sorted(grouped)]


def render(rows: list[RunRow]) -> str:
    out: list[str] = []
    if not rows:
        return "no runs found in runs/\n"

    out.append("EVERY RUN")
    out.append(
        f"  {'run':<34}{'arm':<7}{'level':<8}{'outcome':<14}{'built':<6}{'ship':<6}"
        f"{'tests':<7}{'msgs':>5}{'depl':>6}{'emoji':>7}{'ask':>5}{'files':>8}{'cost':>9}"
    )
    for r in rows:
        files = f"{r.files_touched}/{r.files_needed}"
        emoji = "-" if r.emoji is None else str(r.emoji)
        cost = "-" if r.cost_usd is None else f"${r.cost_usd:.3f}"
        out.append(
            f"  {r.run_id:<34}{r.arm:<7}{r.level:<8}{r.outcome:<14}{r.built:<6}{r.shipped:<6}"
            f"{r.tests:<7}{r.messages:>5}{r.deploys:>6}{emoji:>7}{'YES' if r.asked else 'no':>5}"
            f"{files:>8}{cost:>9}"
        )

    out.append("")
    out.append("ACROSS ARMS - the four things worth comparing")
    out.append(
        f"  {'arm':<7}{'level':<8}{'n':>3}{'ship rate':>11}{'hidden tests':>14}"
        f"{'messages':>10}{'emoji':>7}{'asked':>7}"
    )
    for cell in cells(rows):
        tests = f"{cell.mean_tests:.1f}/{cell.tests_total}" if cell.tests_total else "-"
        emoji = "-" if cell.mean_emoji is None else f"{cell.mean_emoji:.1f}"
        out.append(
            f"  {cell.arm:<7}{cell.level:<8}{cell.n:>3}{cell.ship_rate:>10.0f}%{tests:>14}"
            f"{cell.mean_messages:>10.1f}{emoji:>7}{f'{cell.asked_count}/{cell.n}':>7}"
        )

    thin = [c for c in cells(rows) if c.n < 3]
    if thin:
        out.append("")
        out.append("  NOTE: these cells have fewer than 3 runs, so their numbers are one")
        out.append("        sample, not a measurement: " + ", ".join(f"{c.arm}/{c.level} (n={c.n})" for c in thin))

    models = {r.model for r in rows if r.model}
    if len(models) > 1:
        out.append("")
        out.append("  WARNING: these runs did not all use the same model, so they are not")
        out.append("           comparable: " + ", ".join(sorted(models)))
    return "\n".join(out) + "\n"


def write_csv(rows: list[RunRow], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].as_dict()) if rows else ["run_id"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_lab.summary", description="Tabulate every run in runs/.")
    parser.add_argument("--csv", type=Path, help="also write the per-run table as CSV")
    parser.add_argument("--runs-root", type=Path, help="override the runs/ directory")
    parser.add_argument("--email", action="store_true", help="email the summary using [email] in config.toml")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    args = parser.parse_args(argv)

    rows = load_rows(args.runs_root)
    text = render(rows)
    sys.stdout.write(text)

    if args.csv and rows:
        write_csv(rows, args.csv)
        print(f"wrote {args.csv}")

    if args.email:
        from . import notify

        config = Config.load(args.config)
        record = {"run_id": "summary", "arm": "all", "level": "all", "outcome": f"{len(rows)} runs"}
        sent, why = notify.send_report(record, None, text, config.email)
        print(f"email: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
