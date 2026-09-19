"""Proof that the referee ends runs when it should, and only then.

Every test here drives the real thing: the real runner, the real supervision
loop, a real child process on the other end of a real pipe. The only stand-in
is the agent itself, which is agent_lab/fakeagent.py replaying a script.

Caps come from tests/config.test.toml - the same shape as config.toml, small
enough to fire in seconds.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_lab import runner  # noqa: E402
from agent_lab.arms import available, get_arm, registry  # noqa: E402
from agent_lab.config import REPO_ROOT, Config  # noqa: E402
from agent_lab.events import Action, ActionKind, TranscriptParser  # noqa: E402
from agent_lab.redact import Redactor  # noqa: E402
from agent_lab.referee import KillCondition, Referee  # noqa: E402
from agent_lab.sandbox import RUNS_ROOT, SandboxError, Workspace, assert_inside_runs  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent / "scripts"
TEST_CONFIG = Path(__file__).resolve().parent / "config.test.toml"

# The fake agent never spends these; they exist because agent-lab refuses to
# start a run without credentials, and that rule is under test too.
FAKE_GITHUB_TOKEN = "AGENTLAB-FAKE-GITHUB-TOKEN-0123456789"
FAKE_VERCEL_TOKEN = "AGENTLAB-FAKE-VERCEL-TOKEN-0123456789"


class HarnessCase(unittest.TestCase):
    """Runs the fake arm through the real runner and reads the record."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = Config.load(TEST_CONFIG)
        cls.runs_root = RUNS_ROOT / f"_selftest-{os.getpid()}"
        cls.runs_root.mkdir(parents=True, exist_ok=True)
        cls._saved_env = {k: os.environ.get(k) for k in ("GITHUB_TOKEN", "VERCEL_TOKEN")}
        os.environ["GITHUB_TOKEN"] = FAKE_GITHUB_TOKEN
        os.environ["VERCEL_TOKEN"] = FAKE_VERCEL_TOKEN

    @classmethod
    def tearDownClass(cls) -> None:
        for key, value in cls._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.runs_root, ignore_errors=True)

    def run_script(self, name: str, level: str = "easy") -> tuple[Path, dict]:
        return runner.execute_run(
            level=level,
            arm_name="fake",
            config=self.config,
            extra={"fake_script": str(SCRIPTS / f"{name}.json")},
            runs_root=self.runs_root,
        )


class KillConditions(HarnessCase):
    """One test per condition. All six must fire, and nothing else may."""

    def assert_fired(self, record: dict, condition: KillCondition) -> None:
        self.assertEqual(record["outcome"], condition.value, msg=record.get("outcome_detail"))
        self.assertIsNotNone(record["verdict"], "a kill condition must leave a verdict")
        self.assertEqual(record["verdict"]["condition"], condition.value)

    def test_message_cap(self):
        _, record = self.run_script("message_cap")
        self.assert_fired(record, KillCondition.MESSAGE_CAP)
        self.assertEqual(record["ledger"]["messages"], self.config.referee.message_cap)

    def test_wall_clock(self):
        _, record = self.run_script("wall_clock")
        self.assert_fired(record, KillCondition.WALL_CLOCK)
        self.assertGreaterEqual(record["ledger"]["wall_clock_seconds"], self.config.referee.wall_clock_seconds)
        # It fired on time alone: the agent was silent when it happened.
        self.assertLess(record["ledger"]["messages"], self.config.referee.message_cap)

    def test_deploy_cap(self):
        _, record = self.run_script("deploy_cap")
        self.assert_fired(record, KillCondition.DEPLOY_CAP)
        self.assertEqual(record["ledger"]["deploys"], self.config.referee.deploy_cap)

    def test_silent_loop(self):
        _, record = self.run_script("silent_loop")
        self.assert_fired(record, KillCondition.SILENT_LOOP)
        self.assertIn("index.html", record["verdict"]["detail"])

    def test_silent_loop_resets_on_deploy(self):
        """Six writes to one file, with a deploy in the middle, is not a loop."""
        _, record = self.run_script("silent_loop_reset")
        self.assert_fired(record, KillCondition.AGENT_DONE)
        self.assertEqual(record["ledger"]["files_written"]["index.html"], 5)

    def test_question_loop_in_prose(self):
        _, record = self.run_script("question_loop")
        self.assert_fired(record, KillCondition.QUESTION_LOOP)
        self.assertEqual(record["ledger"]["questions"], self.config.referee.question_cap)

    def test_question_loop_via_tool(self):
        _, record = self.run_script("question_loop_tool")
        self.assert_fired(record, KillCondition.QUESTION_LOOP)

    def test_agent_done(self):
        _, record = self.run_script("agent_done")
        self.assert_fired(record, KillCondition.AGENT_DONE)
        self.assertTrue(record["asked_question"] is False)

    def test_clean_exit_without_saying_done_is_not_a_kill(self):
        """A process that just ends is recorded as such, not as a kill."""
        _, record = self.run_script("quiet_exit")
        self.assertEqual(record["outcome"], "agent_exited")
        self.assertIsNone(record["verdict"])


class KillMechanics(HarnessCase):
    def test_last_five_actions_are_recorded(self):
        _, record = self.run_script("message_cap")
        actions = record["verdict"]["last_actions"]
        self.assertEqual(len(actions), self.config.referee.action_window)
        self.assertTrue(all("kind" in a and "elapsed" in a for a in actions))

    def test_kill_escalates_to_sigkill(self):
        """An agent that ignores SIGTERM still dies."""
        _, record = self.run_script("stubborn")
        self.assertEqual(record["outcome"], KillCondition.WALL_CLOCK.value)
        process = record["processes"][0]
        self.assertTrue(process["killed"])
        # -9 is SIGKILL; None means it had to be abandoned, which would be a bug.
        self.assertEqual(process["exit_code"], -9)

    def test_git_commit_after_every_successful_deploy(self):
        run_dir, record = self.run_script("agent_done")
        self.assertEqual(record["ledger"]["deploy_successes"], 1)
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=run_dir / "workspace",
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("deploy 1", log)

    def test_deploy_url_is_captured(self):
        _, record = self.run_script("agent_done")
        self.assertEqual(record["deploy_url"], "https://fake-done.vercel.app")

    def test_transcript_is_written_as_the_run_happens(self):
        run_dir, record = self.run_script("agent_done")
        transcript = run_dir / "transcript.fake.jsonl"
        self.assertTrue(transcript.exists())
        lines = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
        self.assertGreater(len(lines), 3)
        self.assertEqual(lines[0]["type"], "system")


class Safety(HarnessCase):
    def test_credentials_are_required(self):
        saved = os.environ.pop("VERCEL_TOKEN")
        try:
            with self.assertRaises(runner.PreflightError) as caught:
                self.run_script("agent_done")
            self.assertIn("VERCEL_TOKEN", str(caught.exception))
        finally:
            os.environ["VERCEL_TOKEN"] = saved

    def test_sandbox_refuses_paths_outside_runs(self):
        for bad in ("/tmp", "/etc", str(REPO_ROOT), str(RUNS_ROOT)):
            with self.assertRaises(SandboxError):
                assert_inside_runs(Path(bad))

    def test_credentials_never_reach_the_transcript(self):
        run_dir, record = self.run_script("leaky")
        transcript = (run_dir / "transcript.fake.jsonl").read_text()
        self.assertNotIn(FAKE_VERCEL_TOKEN, transcript)
        self.assertIn("REDACTED", transcript)
        # ...nor the run record, nor the log.
        self.assertNotIn(FAKE_VERCEL_TOKEN, (run_dir / "run.json").read_text())

    def test_agent_environment_excludes_the_harness_session(self):
        from agent_lab.agentproc import build_env

        env = build_env(self.config, {"CLAUDE_CODE_SESSION_ID": "parent", "PATH": "/bin",
                                      "GITHUB_TOKEN": "x" * 20, "SECRET_SAUCE": "nope"})
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("SECRET_SAUCE", env)
        self.assertIn("GITHUB_TOKEN", env)

    def test_prompt_carries_no_caps(self):
        task = runner.load_task("easy")
        runner.check_prompt_is_clean(task.prompt, self.config)
        real = Config.load()
        for value in real.referee.caps_summary().values():
            self.assertNotIn(str(value), task.prompt)

    def test_harness_integrity_is_recorded(self):
        _, record = self.run_script("agent_done")
        self.assertTrue(record["integrity"]["harness_unchanged"])
        self.assertTrue(record["integrity"]["config_unchanged"])


class RefereeUnit(unittest.TestCase):
    """The referee on its own: no processes, no files."""

    def setUp(self) -> None:
        self.config = Config.load(TEST_CONFIG).referee

    def make(self, clock=None) -> Referee:
        referee = Referee(self.config, clock=clock or (lambda: 0.0))
        referee.start()
        return referee

    def test_verdict_is_final(self):
        """Once it has fired, nothing an agent does changes the verdict."""
        referee = self.make()
        for i in range(self.config.message_cap):
            referee.observe(Action(i, 0.0, ActionKind.MESSAGE, "hi"))
        first = referee.verdict
        self.assertEqual(first.condition, KillCondition.MESSAGE_CAP)
        for i in range(50):
            referee.observe(Action(i, 0.0, ActionKind.DONE, "actually I am done"))
        self.assertIs(referee.verdict, first)

    def test_ledger_freezes_at_the_kill(self):
        referee = self.make()
        for i in range(self.config.message_cap + 10):
            referee.observe(Action(i, 0.0, ActionKind.MESSAGE, "hi"))
        self.assertEqual(referee.ledger.messages, self.config.message_cap)

    def test_has_no_way_to_raise_a_cap(self):
        referee = self.make()
        for name in ("message_cap", "deploy_cap", "wall_clock_seconds"):
            with self.assertRaises((AttributeError, TypeError)):
                setattr(referee.caps, name, 10_000)

    def test_thinking_only_messages_are_counted_separately(self):
        """A live arm1 run spent 26 of its 60 messages on thinking alone.

        They count toward the cap - they are messages - but they are also
        counted on their own, because an arm on an extended-thinking model
        spends its budget very differently from one that does not.
        """
        parser = TranscriptParser(Config.load(TEST_CONFIG).referee.detect)
        referee = self.make()
        stream = [
            {"type": "assistant", "parent_tool_use_id": None,
             "message": {"content": [{"type": "thinking", "thinking": "hmm", "signature": "x"}]}},
            {"type": "assistant", "parent_tool_use_id": None,
             "message": {"content": [{"type": "text", "text": "here goes"}]}},
            {"type": "assistant", "parent_tool_use_id": None,
             "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
        ]
        actions = [a for message in stream for a in parser.feed(message, 0.0)]
        for action in actions:
            referee.observe(action)
        self.assertEqual(referee.ledger.messages, 3)
        self.assertEqual(referee.ledger.thinking_messages, 1)
        self.assertEqual([a.detail for a in actions if a.kind is ActionKind.MESSAGE][0], "(thinking)")

    def test_paths_normalize_to_one_file(self):
        """Absolute and relative spellings of one file are one file."""
        workspace = RUNS_ROOT / "_normtest" / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            referee = Referee(self.config, workspace=workspace, clock=lambda: 0.0)
            referee.start()
            for path in ("index.html", str(workspace / "index.html"), "./index.html"):
                referee.observe(Action(1, 0.0, ActionKind.WRITE, "w", path=path))
            self.assertEqual(dict(referee.ledger.files_written), {"index.html": 3})
        finally:
            shutil.rmtree(workspace.parent, ignore_errors=True)


class ArmSeam(unittest.TestCase):
    """Adding an arm must not touch the runner, the referee or the scorer."""

    def test_arms_are_discovered_from_the_filesystem(self):
        self.assertIn("arm1", available())
        self.assertIn("fake", available())

    def test_core_modules_do_not_name_any_arm(self):
        for module in ("runner.py", "referee.py", "scorer.py", "sandbox.py", "agentproc.py"):
            source = (REPO_ROOT / "agent_lab" / module).read_text()
            for arm_name in registry():
                self.assertNotIn(
                    f"arms.{arm_name}", source, f"{module} should not import a specific arm"
                )
                self.assertNotIn(
                    f'"{arm_name}"', source, f"{module} should not mention the arm {arm_name!r} by name"
                )

    def test_a_new_arm_registers_itself(self):
        """Drop a module into the package directory and it becomes runnable."""
        import agent_lab.arms as arms_pkg

        probe = REPO_ROOT / "agent_lab" / "arms" / "probe_arm.py"
        probe.write_text(
            "from .base import Arm, RunResult, register\n\n"
            "@register\n"
            "class ProbeArm(Arm):\n"
            "    name = 'probe'\n"
            "    description = 'temporary test arm'\n"
            "    def run(self, task, workspace, referee):\n"
            "        return RunResult(notes='probe')\n"
        )
        try:
            importlib.invalidate_caches()
            arms_pkg._discovered = False
            self.assertIn("probe", available())
            self.assertEqual(get_arm("probe").name, "probe")
        finally:
            probe.unlink(missing_ok=True)
            registry().pop("probe", None)
            arms_pkg.registry().pop("probe", None)
            from agent_lab.arms.base import _REGISTRY

            _REGISTRY.pop("probe", None)
            arms_pkg._discovered = False
            sys.modules.pop("agent_lab.arms.probe_arm", None)

    def test_unknown_arm_is_a_clear_error(self):
        with self.assertRaises(KeyError) as caught:
            get_arm("arm9")
        self.assertIn("arm9", str(caught.exception))


class DeployDetection(unittest.TestCase):
    """The referee must recognise a deploy however the agent spelled it.

    Regression: the first live arm1 run deployed with
    `npx --yes vercel@latest deploy --prod`. The old pattern assumed
    `npx vercel`, matched nothing, and the referee recorded zero deploys - so
    deploy_cap could never fire and the silent-loop counter never reset.
    """

    DEPLOYS = [
        'npx --yes vercel@latest deploy --prod --yes --token "$VERCEL_TOKEN" 2>&1 | tail -50',
        "npx --yes vercel@32 deploy --prod --yes 2>&1 | tail -60",
        "npx vercel --prod",
        "vercel deploy --prod",
        "vercel --prod",
        "cd site && vercel deploy",
        "VERCEL_TOKEN=x vercel deploy --prod",
        "./node_modules/.bin/vercel deploy",
        "pnpm dlx vercel deploy --prod",
        "npx netlify deploy --prod",
        "wrangler deploy",
        "gh workflow run deploy.yml",
    ]

    NOT_DEPLOYS = [
        "vercel ls",
        "vercel whoami",
        "vercel --version",
        "cat vercel.json",
        'curl -s https://api.vercel.com/v2/user -H "Authorization: Bearer x"',
        "ls /root/.local/share/com.vercel.cli",
        'find / -iname "*vercel*"',
        "grep -r vercel .",
        "npm install vercel",
        "echo deploying to vercel",
    ]

    def setUp(self) -> None:
        self.detect = Config.load().referee.detect

    def matches(self, command: str) -> bool:
        return any(p.search(command) for p in self.detect.deploy_commands)

    def test_real_deploy_commands_are_counted(self):
        for command in self.DEPLOYS:
            with self.subTest(command=command):
                self.assertTrue(self.matches(command), f"missed a deploy: {command}")

    def test_mentions_of_vercel_are_not_deploys(self):
        for command in self.NOT_DEPLOYS:
            with self.subTest(command=command):
                self.assertFalse(self.matches(command), f"false positive: {command}")

    def test_a_deploy_action_comes_out_of_the_stream(self):
        """End to end through the parser, not just the regex."""
        parser = TranscriptParser(self.detect)
        message = {
            "type": "assistant",
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                     "input": {"command": "npx --yes vercel@latest deploy --prod --yes"}}]},
        }
        kinds = [a.kind for a in parser.feed(message, 0.0)]
        self.assertIn(ActionKind.DEPLOY, kinds)


class Redaction(unittest.TestCase):
    def test_known_token_shapes(self):
        redactor = Redactor({})
        for secret in ("ghp_" + "a" * 36, "sk-ant-" + "b" * 40, "AKIA" + "C" * 16):
            self.assertNotIn(secret, redactor.text(f"the token is {secret} ok"))

    def test_redaction_never_corrupts_a_json_line(self):
        """Regression: masking a value must not eat the backslash escaping a quote.

        A real agent grepped the Vercel CLI source, whose minified JavaScript
        contains `explicitToken=parsedArgs.flags[\"--token\"]`. Redacting the
        raw line consumed the backslash, the string ended early, and the
        transcript line stopped being JSON.
        """
        redactor = Redactor({"VERCEL_TOKEN": FAKE_VERCEL_TOKEN})
        raw = json.dumps({
            "text": 'explicitToken=parsedArgs.flags["--token"],tokenSource="flag"',
            "leak": f"deploying with {FAKE_VERCEL_TOKEN}",
        })
        line, parsed = redactor.json_line(raw)
        self.assertIsNotNone(parsed, "a JSON line must come back parsed")
        json.loads(line)  # must not raise
        self.assertNotIn(FAKE_VERCEL_TOKEN, line)

    def test_non_json_lines_still_get_redacted(self):
        redactor = Redactor({"VERCEL_TOKEN": FAKE_VERCEL_TOKEN})
        line, parsed = redactor.json_line(f"plain stderr noise {FAKE_VERCEL_TOKEN}")
        self.assertIsNone(parsed)
        self.assertNotIn(FAKE_VERCEL_TOKEN, line)

    def test_redacted_json_still_parses(self):
        redactor = Redactor({"VERCEL_TOKEN": FAKE_VERCEL_TOKEN})
        line = json.dumps({"cmd": f"vercel --token {FAKE_VERCEL_TOKEN}", "n": 1})
        self.assertEqual(json.loads(redactor.text(line))["n"], 1)
        self.assertNotIn(FAKE_VERCEL_TOKEN, redactor.text(line))


if __name__ == "__main__":
    unittest.main(verbosity=2)
