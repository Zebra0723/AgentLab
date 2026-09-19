"""Credential redaction.

Every byte the harness writes to disk goes through a Redactor: transcripts,
run records, logs. Two layers:

1. Pattern layer - known credential shapes (GitHub tokens, Anthropic keys,
   `--token <value>` flags, private key blocks).
2. Literal layer - the actual values of sensitive environment variables in
   this process. This is what catches an opaque token like VERCEL_TOKEN,
   which has no distinctive shape.

Replacements contain no quotes or backslashes, so redacting a line of JSON
leaves it valid JSON.
"""

from __future__ import annotations

import os
import re
import json
from typing import Any, Iterable

# Environment variables whose *values* are redacted wherever they appear.
SENSITIVE_ENV_EXACT = frozenset({
    "GITHUB_TOKEN",
    "VERCEL_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "NPM_TOKEN",
    "GH_TOKEN",
})

# ...plus anything whose name looks like a secret.
SENSITIVE_ENV_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)$")

# A literal shorter than this is too common to blind-replace ("1", "true").
MIN_LITERAL_LEN = 8

PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "GITHUB_TOKEN"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GITHUB_TOKEN"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "ANTHROPIC_KEY"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "API_KEY"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS_KEY"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "SLACK_TOKEN"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "PRIVATE_KEY"),
    # Credentials passed on a command line or in a URL.
    (re.compile(r"(?i)(--(?:token|api-?key|password|secret)[= ])((?:[^\s\"']{7,})[^\s\"'\\])"), "FLAG"),
    (re.compile(r"(?i)(\b[A-Z_]*(?:TOKEN|SECRET|API_?KEY|PASSWORD)=)((?:[^\s\"';|&]{7,})[^\s\"';|&\\])"), "ENV_ASSIGN"),
    (re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)((?:[^\s\"']{7,})[^\s\"'\\])"), "HEADER"),
    (re.compile(r"https://[^\s/@\"']+:[^\s/@\"']+@"), "URL_CREDS"),
)

_MASK = "***REDACTED-{}***"


class Redactor:
    """Redacts credentials from text and from nested JSON-like objects."""

    def __init__(self, literals: dict[str, str] | None = None) -> None:
        # Longest first: if one secret contains another, mask the bigger one.
        self._literals = sorted(
            ((v, k) for k, v in (literals or {}).items() if v and len(v) >= MIN_LITERAL_LEN),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        extra_names: Iterable[str] = (),
        extra_literals: dict[str, str] | None = None,
    ) -> "Redactor":
        """Build a redactor from the environment.

        `extra_literals` covers a credential the harness was handed directly
        rather than read from the environment - a token passed as an argument
        is still a token, and must not reach an error message.
        """
        env = dict(os.environ if env is None else env)
        names = {n for n in env if n in SENSITIVE_ENV_EXACT or SENSITIVE_ENV_PATTERN.search(n)}
        names.update(extra_names)
        literals = {n: env[n] for n in names if env.get(n)}
        literals.update({k: v for k, v in (extra_literals or {}).items() if v})
        return cls(literals)

    def text(self, value: str) -> str:
        if not value:
            return value
        out = value
        for literal, name in self._literals:
            if literal in out:
                out = out.replace(literal, _MASK.format(name))
        for pattern, name in PATTERNS:
            if name in ("FLAG", "ENV_ASSIGN", "HEADER"):
                out = pattern.sub(lambda m, n=name: m.group(1) + _MASK.format(n), out)
            elif name == "URL_CREDS":
                out = pattern.sub(_MASK.format(name) + "@", out)
            else:
                out = pattern.sub(_MASK.format(name), out)
        return out

    def obj(self, value: Any) -> Any:
        """Recursively redact strings inside dicts/lists/tuples."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value

    def json_line(self, raw: str) -> tuple[str, dict[str, Any] | None]:
        """Redact one line of JSON without ever breaking it.

        Redacting the raw text is unsafe: a replacement can swallow the
        backslash that escapes a quote inside a string, which ends the string
        early and corrupts the line. So parse first, redact the structure, and
        re-serialize. A line that is not JSON falls back to text redaction,
        where there is no escaping to protect.

        Returns (line to write, parsed object or None).
        """
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return self.text(raw), None
        clean = self.obj(parsed)
        return json.dumps(clean, ensure_ascii=False), (clean if isinstance(clean, dict) else None)

    def holds_secret(self, value: str) -> bool:
        """True if `value` still contains something that looks like a credential."""
        return self.text(value) != value
