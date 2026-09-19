"""Workspaces, path safety, and the git trail.

Every agent process runs with its cwd set to a Workspace, which is always
runs/<run_id>/workspace/. `Workspace.create` refuses to build one whose
resolved path falls outside the runs/ tree - including by way of a symlink,
because the check is made after resolution.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT

RUNS_ROOT = REPO_ROOT / "runs"

# Directories that are never counted as agent-authored files.
IGNORED_DIRS = frozenset({".git", "node_modules", ".next", ".vercel", "dist", "build", "__pycache__", ".venv"})


class SandboxError(RuntimeError):
    """Raised when a path would put an agent outside the runs/ tree."""


def assert_inside_runs(path: Path, runs_root: Path | None = None) -> Path:
    """Resolve `path` and refuse it if it is not under runs/."""
    root = (runs_root or RUNS_ROOT).resolve()
    resolved = Path(path).resolve()
    if resolved == root or root not in resolved.parents:
        raise SandboxError(f"sandbox path {resolved} resolves outside the runs/ tree ({root})")
    return resolved


def new_run_id(level: str, arm: str, now: datetime | None = None) -> str:
    """Timestamped and unique: two runs in the same second must not collide."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in s)
    return f"{stamp}-{safe(level)}-{safe(arm)}-{secrets.token_hex(2)}"


@dataclass(frozen=True)
class Workspace:
    """The one directory an agent is allowed to work in."""

    run_id: str
    run_dir: Path
    path: Path

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    @classmethod
    def create(cls, run_id: str, runs_root: Path | None = None) -> "Workspace":
        root = (runs_root or RUNS_ROOT).resolve()
        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / run_id
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        # Check after creation, so a symlinked runs/<id> is caught by resolve().
        resolved = assert_inside_runs(workspace, root)
        return cls(run_id=run_id, run_dir=resolved.parent, path=resolved)

    def transcript_path(self, label: str) -> Path:
        """Transcripts live beside the workspace, not inside it.

        A single-agent arm takes the default label and gets transcript.jsonl.
        An arm that runs several agents labels each one, and each gets its own
        transcript.<label>.jsonl.
        """
        name = "transcript.jsonl" if label in ("", "agent") else f"transcript.{label}.jsonl"
        return self.run_dir / name

    # -- git ---------------------------------------------------------------

    def git_init(self) -> bool:
        if not shutil.which("git"):
            return False
        if (self.path / ".git").exists():
            return True
        ok = self._git("init", "-q") and self._git("commit", "--allow-empty", "-q", "-m", "agent-lab: run start")
        return ok

    def git_commit(self, message: str) -> bool:
        """Commit everything in the workspace. Returns False if nothing changed."""
        if not shutil.which("git") or not (self.path / ".git").exists():
            return False
        self._git("add", "-A")
        return self._git("commit", "-q", "-m", message)

    def _git(self, *args: str) -> bool:
        try:
            done = subprocess.run(
                [
                    "git",
                    "-c", "user.name=agent-lab",
                    "-c", "user.email=agent-lab@localhost",
                    "-c", "commit.gpgsign=false",
                    *args,
                ],
                cwd=self.path,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return done.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # -- inspection --------------------------------------------------------

    def source_files(self) -> list[str]:
        """Files in the workspace that a human would call part of the build."""
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.path):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    out.append(str(full.relative_to(self.path)))
                except ValueError:
                    continue
        return sorted(out)
