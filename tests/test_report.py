"""Proof for the reporting side: the email and the cross-run summary.

No mail is ever sent here. The SMTP layer is never reached, because every test
either has email disabled or has no password in the environment.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_lab import notify, summary  # noqa: E402
from agent_lab.config import Config  # noqa: E402

TEST_CONFIG = Path(__file__).resolve().parent / "config.test.toml"

RECORD = {
    "run_id": "20260919-120000-easy-arm1-aaaa", "arm": "arm1", "level": "easy",
    "outcome": "agent_done", "deploy_url": "https://agent-1-level-1.vercel.app",
    "deploy_target": {"project": "agent-1-level-1"},
    "ledger": {"messages": 14, "deploys": 2, "wall_clock_seconds": 52},
    "asked_question": False, "files": {"touched": 1, "needed": 1},
    "processes": [{"total_cost_usd": 0.04, "model": "claude-sonnet-5"}],
}
SCORE = {
    "built": "PASS", "shipped": "PASS",
    "checks": [{"name": f"c{i}", "status": "PASS"} for i in range(4)],
    "metrics": {"emoji_count": 0},
}


class TheEmail(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config.load(TEST_CONFIG).email

    def test_the_subject_carries_the_verdict(self):
        subject = notify.subject_for(RECORD, SCORE)
        for expected in ("arm1/easy", "BUILT PASS", "SHIPPED PASS", "4/4"):
            self.assertIn(expected, subject)

    def test_an_unscored_run_says_so(self):
        self.assertIn("unscored", notify.subject_for(RECORD, {"built": "UNSCORED", "shipped": "FAIL"}))

    def test_the_body_leads_with_the_deployment(self):
        body = notify.body_for(RECORD, SCORE, "THE LOG")
        self.assertIn("agent-1-level-1", body)
        self.assertIn("https://agent-1-level-1.vercel.app", body)
        self.assertTrue(body.rstrip().endswith("THE LOG"))

    def test_disabled_means_nothing_is_sent(self):
        sent, why = notify.send_report(RECORD, SCORE, "log", self.config, env={"AGENT_LAB_TEST_SMTP_PASSWORD": "x"})
        self.assertFalse(sent)
        self.assertIn("off in config", why)

    def test_a_missing_password_is_reported_not_raised(self):
        enabled = Config.load(TEST_CONFIG).email.__class__(**{**self.config.__dict__, "enabled": True})
        sent, why = notify.send_report(RECORD, SCORE, "log", enabled, env={})
        self.assertFalse(sent)
        self.assertIn("AGENT_LAB_TEST_SMTP_PASSWORD", why)

    def test_the_message_is_built_without_touching_the_network(self):
        message = notify.build_message(RECORD, SCORE, "THE LOG", self.config)
        self.assertEqual(message["To"], self.config.to)
        self.assertIn("THE LOG", message.get_content())


class TheSummary(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def add_run(self, run_id: str, *, arm="arm1", level="easy", shipped="PASS",
                passed=4, messages=14, emoji=0, asked=False, model="claude-sonnet-5") -> None:
        d = self.root / run_id
        d.mkdir(parents=True)
        record = json.loads(json.dumps(RECORD))
        record.update(run_id=run_id, arm=arm, level=level, asked_question=asked)
        record["ledger"]["messages"] = messages
        record["processes"] = [{"total_cost_usd": 0.04, "model": model}]
        (d / "run.json").write_text(json.dumps(record))
        score = {"built": "PASS" if passed == 4 else "FAIL", "shipped": shipped,
                 "checks": [{"name": f"c{i}", "status": "PASS" if i < passed else "FAIL"} for i in range(4)],
                 "metrics": {"emoji_count": emoji}}
        (d / "score.json").write_text(json.dumps(score))

    def test_a_run_without_a_score_still_appears(self):
        d = self.root / "unscored-run"
        d.mkdir()
        (d / "run.json").write_text(json.dumps(RECORD))
        rows = summary.load_rows(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].built, "UNSCORED")

    def test_ship_rate_counts_only_shipped_runs(self):
        self.add_run("r1", shipped="PASS")
        self.add_run("r2", shipped="FAIL")
        self.add_run("r3", shipped="PASS")
        cell = summary.cells(summary.load_rows(self.root))[0]
        self.assertEqual(cell.n, 3)
        self.assertAlmostEqual(cell.ship_rate, 200 / 3, places=1)

    def test_hidden_tests_are_averaged_not_collapsed(self):
        self.add_run("r1", passed=4)
        self.add_run("r2", passed=2)
        cell = summary.cells(summary.load_rows(self.root))[0]
        self.assertEqual(cell.mean_tests, 3.0)
        self.assertEqual(cell.tests_total, 4)

    def test_arms_and_levels_are_kept_apart(self):
        self.add_run("r1", arm="arm1", level="easy")
        self.add_run("r2", arm="arm2", level="easy")
        self.add_run("r3", arm="arm1", level="hard")
        self.assertEqual(
            [(c.arm, c.level) for c in summary.cells(summary.load_rows(self.root))],
            [("arm1", "easy"), ("arm1", "hard"), ("arm2", "easy")],
        )

    def test_thin_cells_are_called_out(self):
        self.add_run("r1")
        self.assertIn("fewer than 3 runs", summary.render(summary.load_rows(self.root)))

    def test_three_runs_are_not_called_out(self):
        for i in range(3):
            self.add_run(f"r{i}")
        self.assertNotIn("fewer than 3 runs", summary.render(summary.load_rows(self.root)))

    def test_mixed_models_are_flagged_as_incomparable(self):
        self.add_run("r1", model="claude-sonnet-5")
        self.add_run("r2", model="claude-opus-5")
        text = summary.render(summary.load_rows(self.root))
        self.assertIn("did not all use the same model", text)

    def test_one_model_raises_no_warning(self):
        self.add_run("r1")
        self.add_run("r2")
        self.assertNotIn("did not all use the same model", summary.render(summary.load_rows(self.root)))

    def test_instruction_following_is_reported(self):
        self.add_run("r1", emoji=3, asked=True)
        self.add_run("r2", emoji=1, asked=False)
        cell = summary.cells(summary.load_rows(self.root))[0]
        self.assertEqual(cell.mean_emoji, 2.0)
        self.assertEqual(cell.asked_count, 1)

    def test_empty_runs_directory_is_not_a_crash(self):
        self.assertIn("no runs found", summary.render(summary.load_rows(self.root)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
