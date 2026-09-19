"""Deployment identity: the harness names and creates the project.

Which project a run deploys to is a fact about the experiment, not a choice for
the agent. Left to the agent, Vercel invents a name per run
(`daily-os-support-phoenix-realm`), so a URL cannot be traced back to an arm
and a level without opening the transcript.

So the harness creates the project itself - `agent-1-level-1` for arm1 on easy
- and writes `.vercel/project.json` into the workspace before the agent starts.
The agent's own `vercel deploy` then lands there without being told to, and
without being able to choose otherwise.

Every endpoint and path is configurable, because Vercel versions its API and a
bump should not need a code change.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import DeployConfig
from .redact import Redactor
from .sandbox import Workspace


class DeployError(RuntimeError):
    """The project could not be named or created. A run must not start."""


@dataclass(frozen=True)
class DeployTarget:
    name: str
    project_id: str
    org_id: str
    created: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"project": self.name, "project_id": self.project_id, "org_id": self.org_id}


def _slug(text: str) -> str:
    """Vercel project names: lowercase, alphanumeric and dashes, <= 100 chars."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)[:100] or "x"


def agent_token(arm: str) -> str:
    """`arm1` -> `1`. An arm with no trailing number keeps its own name."""
    found = re.search(r"(\d+)$", arm or "")
    return found.group(1) if found else _slug(arm)


def level_token(level: str, numbers: dict[str, int]) -> str:
    """`easy` -> `1`, by the mapping in config. An unmapped level keeps its name."""
    number = numbers.get(level)
    return str(number) if number is not None else _slug(level)


def base_name(arm: str, level: str, config: DeployConfig) -> str:
    return _slug(
        config.name_template.format(agent=agent_token(arm), level=level_token(level, config.level_numbers))
    )


def candidate_names(base: str, max_suffix: int) -> Iterator[str]:
    """`agent-1-level-1`, then `agent-1-level-1-2`, `-3`, ... on repeat runs."""
    yield base
    for n in range(2, max(2, max_suffix) + 1):
        yield f"{base}-{n}"


class VercelAPI:
    """The few calls the harness needs. Nothing here logs the token."""

    def __init__(self, token: str, config: DeployConfig) -> None:
        if not token:
            raise DeployError("no Vercel token: cannot create the deployment project")
        self._token = token
        self._config = config
        # The token was handed to us, not read from the environment, so name it
        # explicitly: an API error that echoes the request must not leak it.
        self._redactor = Redactor.from_env(extra_literals={"VERCEL_TOKEN": token})

    def _url(self, path: str) -> str:
        url = self._config.api_base.rstrip("/") + path
        if self._config.team_id:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"teamId": self._config.team_id})
        return url

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._url(path), data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", "agent-lab")
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except (ValueError, OSError):
                return exc.code, None
        except Exception as exc:  # network, DNS, TLS
            raise DeployError(f"could not reach the Vercel API: {self._redactor.text(str(exc))}") from exc

    def project_exists(self, name: str) -> bool:
        status, _ = self._request("GET", f"{self._config.lookup_path}/{urllib.parse.quote(name)}")
        if status == 200:
            return True
        if status == 404:
            return False
        if status in (401, 403):
            raise DeployError(
                f"Vercel rejected the token when looking up {name!r} (HTTP {status}). "
                "Check VERCEL_TOKEN and the team it is scoped to."
            )
        raise DeployError(f"unexpected HTTP {status} looking up project {name!r}")

    def create_project(self, name: str) -> tuple[str, str]:
        status, payload = self._request("POST", self._config.create_path, {"name": name})
        if status in (200, 201) and isinstance(payload, dict):
            project_id = payload.get("id")
            org_id = payload.get("accountId") or payload.get("ownerId") or self._config.team_id
            if not project_id or not org_id:
                raise DeployError(f"Vercel created {name!r} but returned no id/accountId: {payload}")
            return str(project_id), str(org_id)
        if status == 409:
            raise DeployError(f"project {name!r} was taken between the check and the create")
        detail = ""
        if isinstance(payload, dict):
            detail = str((payload.get("error") or {}).get("message") or payload)[:200]
        raise DeployError(f"Vercel refused to create {name!r} (HTTP {status}) {self._redactor.text(detail)}")


def write_link(workspace: Workspace, target: DeployTarget) -> Path:
    """The file the Vercel CLI reads to know which project it is deploying."""
    link_dir = Path(workspace.path) / ".vercel"
    link_dir.mkdir(parents=True, exist_ok=True)
    link = link_dir / "project.json"
    link.write_text(
        json.dumps({"projectId": target.project_id, "orgId": target.org_id}, indent=2) + "\n",
        encoding="utf-8",
    )
    return link


def prepare_target(
    workspace: Workspace,
    arm: str,
    level: str,
    config: DeployConfig,
    token: str,
    api: VercelAPI | None = None,
) -> DeployTarget:
    """Pick the first free name, create the project, and link the workspace."""
    client = api or VercelAPI(token, config)
    base = base_name(arm, level, config)
    for name in candidate_names(base, config.max_suffix):
        if client.project_exists(name):
            continue
        project_id, org_id = client.create_project(name)
        target = DeployTarget(name=name, project_id=project_id, org_id=org_id)
        write_link(workspace, target)
        return target
    raise DeployError(
        f"every name from {base} to {base}-{config.max_suffix} is taken. "
        "Delete some old projects, or raise max_suffix in config.toml."
    )
