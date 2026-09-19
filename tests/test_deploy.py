"""Proof that the harness, not the agent, decides where a run is published.

Nothing here touches the network: the API client's HTTP layer is replaced, so
what is under test is the naming, the collision walk, and the link file the
Vercel CLI reads.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_lab.config import Config  # noqa: E402
from agent_lab.deploy import (  # noqa: E402
    DeployError,
    DeployTarget,
    VercelAPI,
    agent_token,
    base_name,
    candidate_names,
    prepare_target,
    write_link,
)
from agent_lab.sandbox import RUNS_ROOT, Workspace  # noqa: E402

TEST_CONFIG = Path(__file__).resolve().parent / "config.test.toml"


class FakeVercel:
    """Stands in for the API. Remembers what it was asked to create."""

    def __init__(self, taken: set[str] | None = None) -> None:
        self.taken = set(taken or ())
        self.created: list[str] = []

    def project_exists(self, name: str) -> bool:
        return name in self.taken

    def create_project(self, name: str) -> tuple[str, str]:
        self.created.append(name)
        self.taken.add(name)
        return f"prj_{name}", "team_test"


class Naming(unittest.TestCase):
    def setUp(self) -> None:
        self.deploy = Config.load(TEST_CONFIG).deploy

    def test_arm_and_level_become_numbers(self):
        self.assertEqual(base_name("arm1", "easy", self.deploy), "agent-1-level-1")
        self.assertEqual(base_name("arm2", "medium", self.deploy), "agent-2-level-2")
        self.assertEqual(base_name("arm1", "hard", self.deploy), "agent-1-level-3")

    def test_an_arm_without_a_number_keeps_its_name(self):
        self.assertEqual(agent_token("fake"), "fake")
        self.assertEqual(base_name("fake", "easy", self.deploy), "agent-fake-level-1")

    def test_names_are_valid_vercel_slugs(self):
        name = base_name("My Arm!!", "easy", self.deploy)
        self.assertRegex(name, r"^[a-z0-9-]+$")
        self.assertNotIn("--", name)

    def test_repeats_are_numbered_in_order(self):
        self.assertEqual(
            list(candidate_names("agent-1-level-1", 3)),
            ["agent-1-level-1", "agent-1-level-1-2", "agent-1-level-1-3"],
        )


class PickingAName(unittest.TestCase):
    def setUp(self) -> None:
        self.deploy = Config.load(TEST_CONFIG).deploy
        self.workspace = Workspace.create("deploytest", RUNS_ROOT / "_deploytest")
        self.addCleanup(shutil.rmtree, RUNS_ROOT / "_deploytest", ignore_errors=True)

    def target(self, api: FakeVercel) -> DeployTarget:
        return prepare_target(self.workspace, "arm1", "easy", self.deploy, "token", api=api)

    def test_a_first_run_gets_the_plain_name(self):
        api = FakeVercel()
        self.assertEqual(self.target(api).name, "agent-1-level-1")
        self.assertEqual(api.created, ["agent-1-level-1"])

    def test_a_repeat_run_gets_the_next_number(self):
        api = FakeVercel({"agent-1-level-1"})
        self.assertEqual(self.target(api).name, "agent-1-level-1-2")

    def test_runs_keep_climbing(self):
        api = FakeVercel({"agent-1-level-1", "agent-1-level-1-2", "agent-1-level-1-3"})
        self.assertEqual(self.target(api).name, "agent-1-level-1-4")

    def test_running_out_of_names_stops_the_run(self):
        taken = {"agent-1-level-1"} | {f"agent-1-level-1-{n}" for n in range(2, 60)}
        with self.assertRaises(DeployError) as caught:
            self.target(FakeVercel(taken))
        self.assertIn("max_suffix", str(caught.exception))

    def test_the_workspace_is_linked_to_the_project(self):
        target = self.target(FakeVercel())
        link = Path(self.workspace.path) / ".vercel" / "project.json"
        self.assertTrue(link.exists(), "the Vercel CLI reads this to know the project")
        written = json.loads(link.read_text())
        self.assertEqual(written, {"projectId": target.project_id, "orgId": target.org_id})

    def test_the_link_is_not_counted_as_agent_work(self):
        """Harness scaffolding must not inflate files-touched."""
        self.target(FakeVercel())
        self.assertNotIn(".vercel/project.json", self.workspace.source_files())


class ApiErrors(unittest.TestCase):
    """The HTTP layer is replaced; what is tested is how statuses are read."""

    def setUp(self) -> None:
        self.config = Config.load(TEST_CONFIG).deploy
        self.api = VercelAPI("agentlab-fake-token-value", self.config)

    def respond(self, status: int, payload=None) -> None:
        self.api._request = lambda method, path, body=None: (status, payload)

    def test_missing_token_is_refused_up_front(self):
        with self.assertRaises(DeployError):
            VercelAPI("", self.config)

    def test_404_means_the_name_is_free(self):
        self.respond(404)
        self.assertFalse(self.api.project_exists("agent-1-level-1"))

    def test_200_means_the_name_is_taken(self):
        self.respond(200, {"id": "prj_x"})
        self.assertTrue(self.api.project_exists("agent-1-level-1"))

    def test_a_rejected_token_says_so(self):
        self.respond(403)
        with self.assertRaises(DeployError) as caught:
            self.api.project_exists("agent-1-level-1")
        self.assertIn("VERCEL_TOKEN", str(caught.exception))

    def test_a_created_project_returns_its_ids(self):
        self.respond(200, {"id": "prj_abc", "accountId": "team_xyz"})
        self.assertEqual(self.api.create_project("agent-1-level-1"), ("prj_abc", "team_xyz"))

    def test_a_create_that_returns_no_id_is_an_error(self):
        self.respond(200, {"name": "agent-1-level-1"})
        with self.assertRaises(DeployError):
            self.api.create_project("agent-1-level-1")

    def test_the_token_never_appears_in_an_error(self):
        self.respond(400, {"error": {"message": "bad request agentlab-fake-token-value"}})
        with self.assertRaises(DeployError) as caught:
            self.api.create_project("agent-1-level-1")
        self.assertNotIn("agentlab-fake-token-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
