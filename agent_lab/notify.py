"""Email the report when a run finishes.

The report is the morning log, unchanged, with the deployment at the top. It is
sent through plain SMTP so it works with any mail provider; the password comes
from an environment variable and is never written anywhere.

A run is never failed by a mail problem. If the mail cannot be sent, the run
still stands and the reason is printed once.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from .config import EmailConfig
from .redact import Redactor


def subject_for(record: dict[str, Any], score: dict[str, Any] | None) -> str:
    score = score or {}
    checks = score.get("checks") or []
    passed = sum(1 for c in checks if c.get("status") == "PASS")
    tally = f"{passed}/{len(checks)}" if checks else "unscored"
    return (
        f"agent-lab {record.get('arm', '?')}/{record.get('level', '?')} - "
        f"BUILT {score.get('built', 'UNSCORED')} - SHIPPED {score.get('shipped', 'UNSCORED')} - {tally}"
    )


def body_for(record: dict[str, Any], score: dict[str, Any] | None, log_text: str) -> str:
    target = record.get("deploy_target") or {}
    head = [
        f"run      {record.get('run_id', '?')}",
        f"project  {target.get('project') or '(none)'}",
        f"url      {record.get('deploy_url') or '(none)'}",
        f"outcome  {record.get('outcome', '?')}",
        "",
        "-" * 72,
        "",
    ]
    return "\n".join(head) + log_text


def build_message(
    record: dict[str, Any], score: dict[str, Any] | None, log_text: str, config: EmailConfig
) -> EmailMessage:
    redactor = Redactor.from_env()
    message = EmailMessage()
    message["Subject"] = redactor.text(subject_for(record, score))
    message["From"] = config.from_address or config.username
    message["To"] = config.to
    message.set_content(redactor.text(body_for(record, score, log_text)))
    return message


def send_report(
    record: dict[str, Any],
    score: dict[str, Any] | None,
    log_text: str,
    config: EmailConfig,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Returns (sent, reason). Never raises: a mail problem is not a run problem."""
    if not config.enabled:
        return False, "email is off in config.toml"
    if not config.to:
        return False, "no recipient set in [email].to"

    source = dict(os.environ if env is None else env)
    password = source.get(config.password_env, "")
    if not password:
        return False, f"{config.password_env} is not set, so nothing was sent"

    message = build_message(record, score, log_text, config)
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds) as server:
            if config.starttls:
                server.starttls(context=ssl.create_default_context())
            server.login(config.username or config.from_address or config.to, password)
            server.send_message(message)
        return True, f"sent to {config.to}"
    except Exception as exc:
        # Redact first: an SMTP error can quote what it was given.
        return False, Redactor.from_env(extra_literals={"SMTP": password}).text(f"{type(exc).__name__}: {exc}")
